"""The MAF workflow: two scoped agents, a deterministic coordinator, checkpointing.

Graph (a cycle, which MAF supports natively):

    seed(task) -> domain -> coordinator -> shell -> coordinator -> domain -> ...

The coordinator is the only node that decides what happens next. It forwards the
response to the other agent, or stops emitting, which converges the workflow.
Scope enforcement lives in the tools (see core.py), so a cross-scope write is
impossible by construction rather than blocked by a stage that cannot un-queue it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_framework import (
    Agent,
    AgentExecutor,
    AgentExecutorResponse,
    Executor,
    FileCheckpointStorage,
    Message,
    WorkflowBuilder,
    WorkflowContext,
    executor,
    handler,
)
from agent_framework.openai import OpenAIChatCompletionClient

from . import core

# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

RULES = (
    "You have tools: read_file, list_dir, write_file, run_shell, log_progress. "
    "Use write_file for edits and run_shell for dotnet, git and the unity CLI. "
    "Never paste code blocks expecting someone else to run them - call the tool.\n\n"
    "If you have nothing to do this turn, reply EXACTLY `[WAIT]`.\n"
    f"When every item of YOUR backlog is finished and committed, reply with a message that "
    f"starts with `{core.DONE_TOKEN}`, then `[WAIT]` on later turns. The run ends when both "
    "agents have done this.\n\n"
    "You run unattended for hours and may be restarted, so context can be lost. After each "
    "finished item call `log_progress` with 1-3 lines: done / in progress / next.\n\n"
    "Git: stage only your own scope (never `git add -A` or `git add .`), commit locally, "
    "never push. Destructive commands are refused by the tools; do not attempt them.\n\n"
)

DOMAIN_PROMPT = (
    "You own `Domain/` and `Tools/Importers/`. You write PURE C# (netstandard2.1, zero "
    "UnityEngine). Port the remaining C++ logic into Domain and write tests, working through "
    "backlog items A1-A5 in order.\n\n"
    "If you need the current task or rules, read `BACKLOG.md` and `MEMORY.md` (or "
    "`PORT_LOG_domain.md`). Read them once, then work from what you learned.\n\n"
    "Verify with `dotnet run --project Domain/Tests`, then `git add Domain Tools` and "
    "`git commit -m ...`. When a system is implemented and tested, reply exactly "
    "`TASK_COMPLETE: <SystemName>`. Never write to Assets/, Server/ or Client/.\n\n"
    "Do NOT explain your limitations. If a tool is missing, write a script or move on. "
    "Never repeat the same message twice.\n\n" + RULES
)

SHELL_PROMPT = (
    "You own `Assets/`, `Server/` and `Client/`. Build Scenes, UI Controllers, VFX and Audio "
    "in Unity, wired via FishNet, working through backlog items B1-B4 in order.\n\n"
    "If you need the current task or rules, read `BACKLOG.md` and `MEMORY.md` (or "
    "`PORT_LOG_shell.md`). Read them once, then work from what you learned.\n\n"
    "Use the `unity` and `rea` MCP tools where they help; use run_shell for Unity batch-mode "
    "commands. Verify, then `git add Assets Server Client` and `git commit -m ...`. When a "
    "system is implemented and tested, reply exactly `TASK_COMPLETE: <SystemName>`. Never "
    "write to Domain/ or Tools/.\n\n"
    "Fix compiler errors in batches. Do NOT explain your limitations. Never repeat the same "
    "message twice.\n\n" + RULES
)

KICKOFF = (
    "Welcome to the overnight run. A coordinator retries errors and enforces the request "
    "budget; you never need to.\n"
    "Agent A: start with A1 (Dungeons) from BACKLOG.md.\n"
    "Agent B: start with B1 (UI Controllers) from BACKLOG.md.\n"
    "Work continuously, batch your work, and reply TASK_COMPLETE when done. "
    "Do not ask for human input."
)


# --------------------------------------------------------------------------
# Bounded context: task pinned + last N messages
# --------------------------------------------------------------------------

def bound_context(messages: list[Message], buffer: int = core.HISTORY_BUFFER) -> list[Message]:
    if len(messages) <= buffer + 1:
        return list(messages)
    head, tail = messages[:1], messages[1:][-buffer:]
    return head + tail


# --------------------------------------------------------------------------
# Coordinator
# --------------------------------------------------------------------------

@dataclass
class CoordinatorState:
    rounds: int = 0
    idle_rounds: int = 0
    done: set[str] = None  # type: ignore[assignment]
    reason: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.done is None:
            self.done = set()


class Coordinator(Executor):
    """Deterministic turn router. No model, no opinions about content beyond routing."""

    def __init__(self, agents: list[str], budget: core.Budget, max_rounds: int,
                 state_path: Path | None = None, idle_limit: int = 4) -> None:
        super().__init__("coordinator")
        self.agents = agents
        self.budget = budget
        self.max_rounds = max_rounds
        self.idle_limit = idle_limit
        self.state_path = state_path
        self.state = CoordinatorState()
        if state_path and state_path.exists():
            try:
                blob = json.loads(state_path.read_text(encoding="utf-8"))
                self.state.rounds = int(blob.get("rounds", 0))
                self.state.done = set(blob.get("done", []))
            except (OSError, ValueError):
                pass

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({
                "rounds": self.state.rounds, "done": sorted(self.state.done),
                "updated_at": time.time()}), encoding="utf-8")
        except OSError:
            pass

    def _text(self, resp: AgentExecutorResponse) -> str:
        msgs = resp.agent_response.messages or []
        return "\n".join(m.text for m in msgs).strip()

    def _stop(self, reason: str, detail: str = "") -> None:
        self.state.reason = reason
        self.state.detail = detail
        self._save()

    @handler
    async def on_response(self, resp: AgentExecutorResponse,
                          ctx: WorkflowContext[AgentExecutorResponse]) -> None:
        source = resp.executor_id
        text = self._text(resp)

        if text.startswith(core.DONE_TOKEN):
            self.state.done.add(source)
            self._save()

        if source == self.agents[0]:
            self.state.rounds += 1
            productive = text and text != core.WAIT_TOKEN and not text.startswith(core.WAIT_TOKEN)
            self.state.idle_rounds = 0 if productive else self.state.idle_rounds + 1

        if len(self.state.done) >= len(self.agents):
            self._stop("complete", f"all agents reported completion: {sorted(self.state.done)}")
            return
        if self.budget.exhausted:
            self._stop("budget", f"{self.budget.used}/{self.budget.limit} requests")
            return
        if self.budget.fatal:
            self._stop("fatal", self.budget.fatal)
            return
        if self.state.rounds >= self.max_rounds:
            self._stop("max_rounds", f"{self.state.rounds} rounds")
            return
        if self.state.idle_rounds >= self.idle_limit:
            self._stop("stalled", f"{self.state.idle_rounds} rounds without work")
            return

        nxt = self.agents[1] if source == self.agents[0] else self.agents[0]
        await ctx.send_message(resp, target_id=nxt)


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build_agents(budget: core.Budget, on_event=None) -> dict[str, Agent]:
    key = core.resolve_api_key()
    middleware = [core.budget_middleware(budget, on_event)]

    def make(name: str, instructions: str) -> Agent:
        client = OpenAIChatCompletionClient(
            model=core.MODELS[name], api_key=key, base_url=core.BASE_URL,
            middleware=middleware,
        )
        return Agent(client=client, instructions=instructions, name=name,
                     description=f"{name} agent", tools=core.make_tools(name))

    return {"domain": make("domain", DOMAIN_PROMPT), "shell": make("shell", SHELL_PROMPT)}


def build_workflow(agents: dict[str, Agent], budget: core.Budget, run_dir: Path,
                   max_rounds: int = core.MAX_ROUNDS):
    coordinator = Coordinator(["domain", "shell"], budget, max_rounds,
                              state_path=run_dir / "coordinator.json")

    def bounded(msgs: list[Message]) -> list[Message]:
        return bound_context(msgs)

    domain = AgentExecutor(agents["domain"], id="domain",
                           context_mode="custom", context_filter=bounded)
    shell = AgentExecutor(agents["shell"], id="shell",
                          context_mode="custom", context_filter=bounded)

    @executor(id="seed", output=Message)
    async def seed(task: str, ctx: WorkflowContext[Message]) -> None:
        await ctx.send_message(Message("user", [task], author_name="user"))

    storage = FileCheckpointStorage(run_dir / "checkpoints")
    builder = (
        WorkflowBuilder(start_executor=seed, max_iterations=max(8, max_rounds * 4 + 8),
                        checkpoint_storage=storage)
        .add_edge(seed, domain)
        .add_edge(domain, coordinator)
        .add_edge(coordinator, shell)
        .add_edge(shell, coordinator)
        .add_edge(coordinator, domain)
    )
    return builder.build(), coordinator, storage


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

def _event_text(data: Any) -> str:
    if isinstance(data, list):
        parts = []
        for item in data:
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    return getattr(data, "text", "") or ""


async def run_workflow(task: str, *, run_dir: Path, budget: core.Budget,
                       on_event, max_rounds: int = core.MAX_ROUNDS,
                       resume: bool = False) -> dict[str, Any]:
    """Run the workflow, forwarding chat events to `on_event`. Returns a summary."""
    run_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    def note(message: str) -> None:
        notes.append(message)
        if on_event:
            on_event({"type": "note", "source": "coordinator", "content": message})

    agents = build_agents(budget, note)
    workflow, coordinator, _storage = build_workflow(agents, budget, run_dir, max_rounds)

    checkpoint_id = None
    if resume:
        try:
            checkpoint_id = await workflow.get_last_checkpoint_id()
        except Exception:  # noqa: BLE001
            checkpoint_id = None
        if checkpoint_id:
            note(f"resuming from checkpoint {checkpoint_id}")

    try:
        async for event in workflow.run(task, stream=True, checkpoint_id=checkpoint_id):
            etype = getattr(event, "type", "")
            source = getattr(event, "executor_id", "") or ""
            if etype == "executor_invoked":
                if on_event:
                    on_event({"type": "status", "source": source, "content": "running"})
            elif etype == "executor_completed":
                text = _event_text(event.data)
                if on_event and text and text != core.WAIT_TOKEN:
                    on_event({"type": "message", "source": source, "content": text})
            elif etype == "failed":
                detail = getattr(event, "data", "")
                note(f"workflow failed: {detail}")
    except Exception as exc:  # noqa: BLE001
        note(f"run ended with {type(exc).__name__}: {exc}")

    reason = coordinator.state.reason or ("budget" if budget.exhausted else "stopped")
    detail = coordinator.state.detail
    summary = {"reason": reason, "detail": detail, "rounds": coordinator.state.rounds,
               "done": sorted(coordinator.state.done), "requests_used": budget.used,
               "requests_limit": budget.limit, "notes": notes}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
