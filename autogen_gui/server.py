"""
AutoGen overnight-run backend for the Flyff port project.

Four participants in a RoundRobinGroupChat:
  - Agent A "domain"  (deepseek-v4-pro)      owns Domain/, Tools/Importers/
  - Agent B "shell"   (deepseek-v4.1-flash)  owns Assets/, Server/, Client/
  - Executor          (no model)             runs the agents' ```python / ```sh blocks
  - Gate              (no model)             blocks cross-scope writes

Both agents are attached to BOTH MCP servers (rea + unity) plus their local file
tools, via a CompositeWorkbench (AutoGen forbids `tools=` together with `workbench=`).

Run:
  python -m uvicorn server:app --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv

from autogen_agentchat.agents import AssistantAgent, CodeExecutorAgent
from autogen_agentchat.base import ChatAgent, Response
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.messages import (
    BaseAgentEvent,
    BaseChatMessage,
    TextMessage,
)
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core import EVENT_LOGGER_NAME, CancellationToken
from autogen_core.code_executor import CodeBlock, CodeResult
from autogen_core.model_context import BufferedChatCompletionContext
from autogen_core.tools import (
    BaseTool,
    StaticWorkbench,
    ToolResult,
    ToolSchema,
    Workbench,
)
from autogen_ext.code_executors.local import LocalCommandLineCodeExecutor
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.tools.mcp import McpWorkbench, StdioServerParams

WORKSPACE = Path(__file__).resolve().parent
# Target repository the agents work on. Override with FLYFF_PROJECT_ROOT.
PROJECT_ROOT = Path(os.getenv("FLYFF_PROJECT_ROOT", r"D:\Flyff source code"))

# Model ids are resolved from the environment further below (after .env loads).

# Per-agent write scopes (top-level dirs).
WRITE_SCOPES: dict[str, tuple[str, ...]] = {
    "domain": ("Domain", "Tools"),
    "shell": ("Assets", "Server", "Client"),
}
BLOCKED_DIRS = {"Library", "Temp", "Builds", "Logs", "UserSettings", "obj", "bin",
                "PatchStage", "Copy Files.bat"}

MAX_READ_BYTES = 120_000
MAX_LIST_ENTRIES = 200
SHELL_TIMEOUT = 600
MCP_READ_TIMEOUT = 60          # npx/unity cold start exceeds AutoGen's 5s default
HISTORY_BUFFER = 12            # messages of history replayed per LLM turn

MCP_SERVERS = {
    "rea": StdioServerParams(
        command="npx", args=["-y", "rea-agents@6.4.0", "mcp"],
        read_timeout_seconds=MCP_READ_TIMEOUT,
    ),
    "unity": StdioServerParams(
        command="unity", args=["mcp", "--project-path", str(PROJECT_ROOT)],
        read_timeout_seconds=MCP_READ_TIMEOUT,
    ),
}

WAIT_TOKEN = "[WAIT]"

# Catastrophic-only denylist (not a security boundary — the owner runs trusted
# agents on this machine; this only blocks drive-wrecking patterns).
SHELL_DENYLIST = (
    "rm -rf /", "rm -rf ~", "rm -rf c:", "rm -rf d:", "format ",
    "del /s /q c:\\", "del /s /q d:\\", "rd /s /q c:\\", "rd /s /q d:\\",
    "remove-item -recurse c:\\", "remove-item -recurse d:\\", "diskpart",
    "mkfs", "dd if=", ":(){:|:&};:",
)

app = FastAPI(title="AutoGen Overnight Run - Flyff port")

# Load .env sitting next to this file (or in the project root above it).
for _candidate in (WORKSPACE / ".env", WORKSPACE.parent / ".env"):
    if _candidate.exists():
        load_dotenv(_candidate)
        break
else:
    load_dotenv()  # fall back to cwd/.env


def resolve_api_key() -> str:
    """API key comes from the environment only — never from source."""
    key = os.getenv("DEEPSEEK_API_KEY", "").strip() or os.getenv("AIBH_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "DEEPSEEK_API_KEY is not set.\n"
            "Copy .env.example to .env and fill in your key, then restart."
        )
    return key


def _env(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


API_KEY = resolve_api_key()
BASE_URL = _env("DEEPSEEK_BASE_URL", "https://www.aibh.cc/v1")
MODEL_PRO = _env("DEEPSEEK_PRO_MODEL", "deepseek-v4-pro")
MODEL_FLASH = _env("DEEPSEEK_FLASH_MODEL", "deepseek-v4.1-flash")
MODELS = {"domain": MODEL_PRO, "shell": MODEL_FLASH}


# --------------------------------------------------------------------------
# Path safety
# --------------------------------------------------------------------------

def safe_path(agent: str, rel: str, *, for_write: bool) -> Path:
    root = PROJECT_ROOT.resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root):
        raise PermissionError(f"Path escapes project root: {rel}")
    rel_parts = p.relative_to(root).parts
    if rel_parts and rel_parts[0] in BLOCKED_DIRS:
        raise PermissionError(f"'{rel_parts[0]}/' is off-limits for agent tools.")
    if for_write:
        scope = WRITE_SCOPES[agent]
        if not rel_parts or rel_parts[0] not in scope:
            raise PermissionError(f"{agent} may only write inside {scope}; got '{rel}'")
        if p.exists() and p.is_dir():
            raise IsADirectoryError(rel)
    return p


# --------------------------------------------------------------------------
# Local tools
# --------------------------------------------------------------------------

def list_dir(rel: str = ".") -> str:
    """List one directory of the Flyff project. Directories end with '/'."""
    p = safe_path("domain", rel, for_write=False)
    if not p.exists():
        return f"NOT FOUND: {rel}"
    if not p.is_dir():
        return f"NOT A DIRECTORY: {rel}"
    entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    lines = [e.name + ("/" if e.is_dir() else "") for e in entries[:MAX_LIST_ENTRIES]]
    if len(entries) > MAX_LIST_ENTRIES:
        lines.append(f"... ({len(entries) - MAX_LIST_ENTRIES} more)")
    return "\n".join(lines) if lines else "(empty)"


def read_file(rel: str, max_lines: int = 200) -> str:
    """Read a text file from the project (UTF-8, capped)."""
    p = safe_path("domain", rel, for_write=False)
    if not p.exists():
        return f"NOT FOUND: {rel}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"READ ERROR: {exc}"
    lines = text.splitlines()
    head = lines[: max(1, min(max_lines, 400))]
    note = f"\n[... {len(lines) - len(head)} more lines]" if len(lines) > len(head) else ""
    return "\n".join(head) + note


def make_write_file(agent: str) -> BaseTool:
    def write_file(rel: str, content: str) -> str:
        """Create/overwrite one file inside your scope, in a single call."""
        p = safe_path(agent, rel, for_write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"WROTE {p.relative_to(PROJECT_ROOT)} ({len(content)} chars)"

    from autogen_core.tools import FunctionTool

    return FunctionTool(
        write_file, name=f"write_{agent}_file",
        description=(
            f"Create or overwrite one file inside your scope "
            f"({', '.join(WRITE_SCOPES[agent])}/). Provide the full path relative "
            f"to the project root and the complete file content."
        ),
    )


# --------------------------------------------------------------------------
# Composite workbench: local tools + both MCP servers
# --------------------------------------------------------------------------

def _schema_name(t: Any) -> str:
    """ToolSchema is a TypedDict, so tool schemas are plain dicts here."""
    if isinstance(t, dict):
        return str(t.get("name", ""))
    return str(getattr(t, "name", ""))


def _as_tool_schema(t: Any) -> dict[str, Any]:
    if isinstance(t, dict):
        return {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "parameters": t.get("parameters") or t.get("inputSchema") or {},
        }
    return {
        "name": getattr(t, "name", ""),
        "description": getattr(t, "description", ""),
        "parameters": getattr(t, "parameters", {}) or {},
    }


class CompositeWorkbench(Workbench):
    """Merge local FunctionTools with one or more MCP workbenches into a single
    workbench (AutoGen forbids `tools=` alongside `workbench=`)."""

    def __init__(self, local_tools: Sequence[BaseTool[Any, Any]],
                 mcp: Mapping[str, Workbench]) -> None:
        self._local = StaticWorkbench(list(local_tools))
        self._mcp = dict(mcp)

    async def start(self) -> None:
        await self._local.start()
        for wb in self._mcp.values():
            await wb.start()

    async def stop(self) -> None:
        for wb in self._mcp.values():
            try:
                await wb.stop()
            except Exception:  # noqa: BLE001
                pass
        await self._local.stop()

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in await self._local.list_tools():
            schema = _as_tool_schema(raw)
            tools.append(schema)
            seen.add(schema["name"])
        for name, wb in self._mcp.items():
            try:
                remote = await wb.list_tools()
            except Exception:  # noqa: BLE001  MCP server down: keep the rest usable
                continue
            for raw in remote:
                schema = _as_tool_schema(raw)
                if not schema["name"] or schema["name"] in seen:
                    continue
                if not schema["description"]:
                    schema["description"] = f"[{name} MCP]"
                tools.append(schema)
                seen.add(schema["name"])
        return tools

    async def reset(self) -> None:
        await self._local.reset()
        for wb in self._mcp.values():
            try:
                await wb.reset()
            except Exception:  # noqa: BLE001
                pass

    async def save_state(self) -> Mapping[str, Any]:
        return {}

    async def load_state(self, state: Mapping[str, Any]) -> None:
        return None

    async def call_tool(self, name: str, arguments=None, cancellation_token=None,
                        call_id=None) -> ToolResult:
        local_names = {_schema_name(t) for t in await self._local.list_tools()}
        if name in local_names:
            return await self._local.call_tool(name, arguments, cancellation_token, call_id)
        for wb in self._mcp.values():
            try:
                if name in {_schema_name(t) for t in await wb.list_tools()}:
                    return await wb.call_tool(name, arguments, cancellation_token, call_id)
            except Exception:  # noqa: BLE001
                continue
        return ToolResult(name=name, result=[], is_error=True)


# --------------------------------------------------------------------------
# Scope-enforcing executor + Gate participant
# --------------------------------------------------------------------------

_CODE_BLOCK_RE = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)
_PATH_WRITE_RE = re.compile(
    r"""(?:>>?|(?:Set-Content|Add-Content|Out-File|tee|cp|copy|mv|move|"""
    r"""New-Item|Remove-Item|rm|del))\s+(?:-\w+\s+)*["']?"""
    r"""([A-Za-z]:[\\/][^\s"'<>|]+|(?:Domain|Assets|Server|Client|Tools)[\\/][^\s"'<>|]*)""",
    re.IGNORECASE,
)
# cp/mv move: the *destination* is the last argument, not the first.
_CMD_DEST_RE = re.compile(
    r"""\b(cp|copy|mv|move)\b[^\n;|]*?["']?"""
    r"""([A-Za-z]:[\\/][^\s"'<>|]+|[A-Za-z0-9_.\\/-]+[\\/][^\s"'<>|]*)["']?\s*$""",
    re.IGNORECASE | re.MULTILINE,
)


def _norm_top(raw: str) -> str:
    norm = raw.replace("\\", "/")
    root = str(PROJECT_ROOT).replace("\\", "/").lower()
    norm = norm.lower().replace(root + "/", "").replace(root, "")
    norm = re.sub(r"^[a-zA-Z]:/", "", norm)
    return norm.split("/")[0]


SCOPED_TOPS = ("domain", "assets", "server", "client", "tools")


def scope_violation(text: str, agent: str) -> str | None:
    """Return a description if `text` tries to write outside the agent's scope."""
    allowed = {s.lower() for s in WRITE_SCOPES[agent]}
    targets = [m.group(1) for m in _PATH_WRITE_RE.finditer(text)]
    targets += [m.group(2) for m in _CMD_DEST_RE.finditer(text)]
    for raw in targets:
        top = _norm_top(raw)
        if top in SCOPED_TOPS and top not in allowed:
            return f"'{top.capitalize()}/' is outside {agent}'s scope {WRITE_SCOPES[agent]}"
    return None


class _StatelessAgent(ChatAgent):
    """ChatAgent with no-op lifecycle: these participants hold no state to persist."""

    async def on_reset(self, cancellation_token) -> None:
        return None

    async def on_pause(self, cancellation_token) -> None:
        return None

    async def on_resume(self, cancellation_token) -> None:
        return None

    async def save_state(self) -> Mapping[str, Any]:
        return {}

    async def load_state(self, state: Mapping[str, Any]) -> None:
        return None

    async def close(self) -> None:
        return None

    async def on_messages(self, messages, cancellation_token) -> Response:
        async for m in self.on_messages_stream(messages, cancellation_token):
            if isinstance(m, Response):
                return m
        raise AssertionError("no response")

    @property
    def produced_message_types(self) -> Sequence[type[BaseChatMessage]]:
        return (TextMessage,)


class GateAgent(_StatelessAgent):
    """Deterministic guard between agents and the executor.

    CodeExecutorAgent cannot tell which agent authored a code block (it reads the
    whole thread), so this participant inspects the previous message and refuses
    to let cross-scope writes reach the executor.
    """

    def __init__(self, name: str = "gate") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Blocking stage between agents and the executor."

    async def on_messages_stream(self, messages, cancellation_token):
        last = messages[-1] if messages else None
        source = getattr(last, "source", "")
        body = str(getattr(last, "content", "") or "")
        if source in WRITE_SCOPES:
            blocks = _CODE_BLOCK_RE.findall(body)
            if blocks:
                joined = "\n".join(code for _, code in blocks)
                bad = scope_violation(joined, source)
                if bad:
                    yield Response(chat_message=TextMessage(
                        source=self._name,
                        content=f"BLOCKED: {source} attempted a cross-scope write — {bad}. "
                                f"Nothing was executed. {source}: stay inside "
                                f"{WRITE_SCOPES[source]}.",
                    ))
                    return
        # Nothing to say: stay silent (no cost, no chat noise).
        yield Response(chat_message=TextMessage(source=self._name, content=""))


class IncrementalExecutor(CodeExecutorAgent):
    """CodeExecutorAgent that only executes code blocks NEW since its last turn.

    Without this, every turn re-extracts and re-runs every historical code block
    in the thread (re-running git commit / dotnet / MCP calls each cycle).
    """

    async def extract_code_blocks_from_messages(self, messages) -> list[CodeBlock]:
        start = 0
        for i, m in enumerate(messages):
            if getattr(m, "source", "") == self.name:
                start = i + 1
        fresh = list(messages[start:])
        return await super().extract_code_blocks_from_messages(fresh)


class SafeLocalExecutor(LocalCommandLineCodeExecutor):
    """Local executor with a catastrophic-pattern denylist."""

    async def execute_code_blocks(self, code_blocks, cancellation_token) -> CodeResult:
        for block in code_blocks:
            low = " " + block.code.lower() + " "
            if any(pat in low for pat in SHELL_DENYLIST):
                return CodeResult(
                    exit_code=1,
                    output=f"BLOCKED: code matches a denied destructive pattern:\n{block.code[:200]}",
                )
        return await super().execute_code_blocks(code_blocks, cancellation_token)


# --------------------------------------------------------------------------
# [WAIT] economics: a WAIT-returning stand-in per agent
# --------------------------------------------------------------------------

def make_waitable(agent: AssistantAgent, name: str) -> ChatAgent:
    """Wrap an agent so its LLM turn is skipped while it is waiting.

    The team always cycles domain -> shell -> gate -> executor. When an agent
    replies exactly [WAIT], the next turn of *that* agent yields [WAIT] again
    without an API request, until the thread shows the other agent did work.
    """

    class WaitableAgent(_StatelessAgent):
        def __init__(self) -> None:
            self._agent = agent
            self._waiting = False
            self._name = name

        @property
        def name(self) -> str:
            return self._name

        @property
        def description(self) -> str:
            return f"Agent {name} (skips turns while waiting)."

        @property
        def produced_message_types(self) -> Sequence[type[BaseChatMessage]]:
            return self._agent.produced_message_types

        async def on_messages_stream(self, messages, cancellation_token):
            # Reactivate only if the *other* agent produced non-WAIT output since
            # our own last turn. Scanning past our own last turn avoids being
            # woken by stale history.
            history = [m for m in messages if isinstance(m, TextMessage)]
            last_self = -1
            for i, m in enumerate(history):
                if m.source == self._name:
                    last_self = i
            for m in history[last_self + 1:]:
                if m.source in ("gate", "executor", self._name):
                    continue
                if m.content.strip() != WAIT_TOKEN:
                    self._waiting = False
                    break
            if self._waiting:
                yield Response(chat_message=TextMessage(
                    source=self._name, content=WAIT_TOKEN))
                return

            async for item in self._agent.on_messages_stream(messages, cancellation_token):
                if isinstance(item, Response):
                    if str(item.chat_message.content or "").strip() == WAIT_TOKEN:
                        self._waiting = True
                yield item

        async def on_reset(self, cancellation_token) -> None:
            self._waiting = False
            await self._agent.on_reset(cancellation_token)

        async def on_pause(self, cancellation_token) -> None:
            await self._agent.on_pause(cancellation_token)

        async def on_resume(self, cancellation_token) -> None:
            await self._agent.on_resume(cancellation_token)

        async def save_state(self) -> Mapping[str, Any]:
            return await self._agent.save_state()

        async def load_state(self, state: Mapping[str, Any]) -> None:
            await self._agent.load_state(state)

        async def close(self) -> None:
            await self._agent.close()

    return WaitableAgent()


def build_team(allow_write: bool = True, max_rounds: int = 80) -> RoundRobinGroupChat:
    def local_tools(agent: str) -> list[BaseTool[Any, Any]]:
        tools: list[BaseTool[Any, Any]] = [
            _fn(list_dir, "list_dir", "List a directory of the project."),
            _fn(read_file, "read_file",
                "Read a text file from the project (max_lines caps the output)."),
        ]
        if allow_write:
            tools.append(make_write_file(agent))
        return tools

    agent_a = AssistantAgent(
        "domain", model_client=_client(MODELS["domain"]),
        workbench=CompositeWorkbench(
            local_tools("domain"),
            {n: McpWorkbench(p) for n, p in MCP_SERVERS.items()},
        ),
        # Cap the replayed history: without this, every turn resends the entire
        # transcript and cost grows with turn count.
        model_context=BufferedChatCompletionContext(buffer_size=HISTORY_BUFFER),
        system_message=DOMAIN_SYSTEM,
    )
    agent_b = AssistantAgent(
        "shell", model_client=_client(MODELS["shell"]),
        workbench=CompositeWorkbench(
            local_tools("shell"),
            {n: McpWorkbench(p) for n, p in MCP_SERVERS.items()},
        ),
        model_context=BufferedChatCompletionContext(buffer_size=HISTORY_BUFFER),
        system_message=SHELL_SYSTEM,
    )
    executor = IncrementalExecutor(
        "executor",
        code_executor=SafeLocalExecutor(work_dir=str(PROJECT_ROOT), timeout=SHELL_TIMEOUT),
    )
    gate = GateAgent("gate")

    return RoundRobinGroupChat(
        [make_waitable(agent_a, "domain"), make_waitable(agent_b, "shell"), gate, executor],
        termination_condition=(
            TextMentionTermination("OVERNIGHT_COMPLETE")
            | TextMentionTermination("BUDGET_EXHAUSTED")
            | MaxMessageTermination(max(2, min(max_rounds, 150)))
        ),
    )


def _fn(fn: Any, name: str, description: str) -> BaseTool:
    from autogen_core.tools import FunctionTool
    return FunctionTool(fn, name=name, description=description)


def _client(model: str) -> OpenAIChatCompletionClient:
    return OpenAIChatCompletionClient(
        model=model, api_key=API_KEY, base_url=BASE_URL,
        max_retries=6,
        model_info={"vision": False, "function_calling": True, "json_output": True,
                    "family": "unknown", "structured_output": False},
    )


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

NOISE_RULES = (
    f"If you have nothing to do this turn, reply EXACTLY `{WAIT_TOKEN}`. "
    "If you finish the entire backlog, reply `OVERNIGHT_COMPLETE`."
)

DOMAIN_SYSTEM = (
    "You own `Domain/` and `Tools/Importers/`. You write PURE C# (netstandard2.1, "
    "zero UnityEngine). Your job is to port the remaining C++ logic into the Domain "
    "and write tests, working through backlog items A1-A5 in order.\n\n"
    "If you need to know the current task or rules, you can use your tools to read "
    "`BACKLOG.md` and `MEMORY.md` (or `PORT_LOG_domain.md` / `DECISIONS.md`). Do not "
    "re-read them every turn — read once, then work from what you learned.\n\n"
    "You have access to a terminal via the Executor. Put Python or bash in "
    "```python or ```sh code blocks and the Executor runs them and returns the raw "
    "output. You must run `dotnet run --project Domain/Tests` to verify your work. "
    "You must run `git commit` to save your work. You also have MCP tools from the "
    "`rea` (reverse-engineering) and `unity` (Editor control) servers.\n\n"
    "To save requests: read entire C++ files at once, write complete C# files in a "
    "single request, and batch shell commands into one script per turn. When a "
    "system is fully implemented and tested, reply exactly: "
    "`TASK_COMPLETE: <SystemName>`. Never modify `Assets/`.\n\n"
    "Do NOT explain your limitations. If you do not have a tool, write a script to "
    "accomplish the goal or move on. NEVER repeat the same message twice. "
    "If you are stuck, reply FAILED and wait for the Executor.\n\n"
    + NOISE_RULES
)

SHELL_SYSTEM = (
    "You own `Assets/`, `Server/`, and `Client/`. Your job is to build Scenes, UI "
    "Controllers, VFX, and Audio in Unity, and wire them via FishNet, working through "
    "backlog items B1-B4 in order.\n\n"
    "If you need to know the current task or rules, you can use your tools to read "
    "`BACKLOG.md` and `MEMORY.md` (or `PORT_LOG_shell.md` / `DECISIONS.md`). Do not "
    "re-read them every turn — read once, then work from what you learned.\n\n"
    "You have access to a terminal via the Executor. Put Python or bash in "
    "```python or ```sh code blocks and the Executor runs them and returns the raw "
    "output — e.g. Unity batch-mode CLI commands. You must run `git commit` to save "
    "your work. You also have MCP tools from the `unity` server (160 Editor commands: "
    "add_component, open_scene, build, capture_scene_view, ...) and the `rea` server. "
    "Prefer the `unity` MCP tools over raw CLI where possible.\n\n"
    "Fix compiler errors in batches. When a system is fully implemented and tested, "
    "reply exactly: `TASK_COMPLETE: <SystemName>`. Never modify `Domain/`.\n\n"
    "Do NOT explain your limitations. If you do not have a tool, write a script to "
    "accomplish the goal or move on. NEVER repeat the same message twice. "
    "If you are stuck, reply FAILED and wait for the Executor.\n\n"
    + NOISE_RULES
)

KICKOFF_MESSAGE = (
    "Welcome to the overnight run. You have a 5,000 request budget. \n"
    "Agent A: Start with A1 (Dungeons) from BACKLOG.md.\n"
    "Agent B: Start with B1 (UI Controllers) from BACKLOG.md.\n"
    "Work continuously, batch your code generation, and reply TASK_COMPLETE when "
    "done. Do not ask for human input."
)


# --------------------------------------------------------------------------
# LLM trace
# --------------------------------------------------------------------------

def _trim(value: Any, limit: int = 600) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s if len(s) <= limit else s[:limit] + f" ...[{len(s)} chars total]"


def trace_payload(event: Any) -> dict[str, Any] | None:
    try:
        d = json.loads(str(event))
    except Exception:  # noqa: BLE001
        return None
    kind = d.get("type")
    out: dict[str, Any] = {"kind": kind, "agent": d.get("agent_id")}
    if kind == "LLMStreamStart":
        out["messages"] = [{"role": m.get("role"), "preview": _trim(m.get("content", ""), 220)}
                           for m in d.get("messages", [])]
        return out
    resp = d.get("response", {})
    choices = resp.get("choices", [])
    msg = choices[0].get("message", {}) if choices else {}
    out["reply"] = _trim(msg.get("content") or "", 900)
    out["tool_calls"] = [{"name": c.get("function", {}).get("name"),
                          "arguments": _trim(c.get("function", {}).get("arguments", ""), 300)}
                         for c in msg.get("tool_calls", []) or []]
    out["prompt_tokens"] = d.get("prompt_tokens")
    out["completion_tokens"] = d.get("completion_tokens")
    if kind == "LLMCall":
        out["messages"] = [{"role": m.get("role"), "preview": _trim(m.get("content", ""), 220)}
                           for m in d.get("messages", [])]
    return out


class TraceHandler(logging.Handler):
    def __init__(self, ws: WebSocket) -> None:
        super().__init__(level=logging.INFO)
        self._ws = ws

    def emit(self, record: logging.LogRecord) -> None:
        payload = trace_payload(record.msg)
        if payload is None:
            return
        asyncio.get_running_loop().create_task(self._push(payload))

    async def _push(self, payload: dict[str, Any]) -> None:
        try:
            await self._ws.send_json({"type": "trace", **payload})
        except Exception:  # noqa: BLE001
            pass


def serialize(msg: Any) -> dict[str, Any]:
    t = type(msg).__name__
    out: dict[str, Any] = {"type": t, "source": getattr(msg, "source", "system")}
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        out["content"] = content
        return out
    calls = []
    for c in content:
        if hasattr(c, "name") and hasattr(c, "arguments"):
            calls.append({"name": c.name, "arguments": c.arguments})
        elif isinstance(c, dict):
            calls.append({"name": c.get("name", "?"), "arguments": c.get("arguments", "")})
        else:
            calls.append({"name": "?", "arguments": str(c)})
    out["tool_calls"] = calls
    if t == "ToolCallExecutionEvent":
        out["content"] = "\n".join(
            f"{getattr(r, 'name', '?')} -> {getattr(r, 'content', '')}"
            + (" [ERROR]" if getattr(r, "is_error", False) else "")
            for r in content
        )
    return out


async def run_agent_task(ws: WebSocket, task: str, allow_write: bool,
                         max_rounds: int, trace: bool) -> None:
    team = build_team(allow_write=allow_write, max_rounds=max_rounds)
    cancel_token = CancellationToken()
    handler: TraceHandler | None = None
    if trace:
        ev_logger = logging.getLogger(EVENT_LOGGER_NAME)
        ev_logger.setLevel(logging.INFO)  # root default WARNING drops INFO events
        handler = TraceHandler(ws)
        ev_logger.addHandler(handler)

    async def pump() -> None:
        try:
            async for msg in team.run_stream(task=task, cancellation_token=cancel_token):
                if getattr(msg, "stop_reason", None):
                    await ws.send_json({"type": "done", "stop_reason": msg.stop_reason})
                    return
                # Chat filter: drop executor "no code blocks" noise and [WAIT]/empty.
                text = str(getattr(msg, "content", "") or "").strip()
                if text in ("", WAIT_TOKEN):
                    continue
                if getattr(msg, "source", "") == "executor" and text.startswith("No code blocks"):
                    continue
                await ws.send_json(serialize(msg))
            await ws.send_json({"type": "done", "stop_reason": "run finished"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            try:
                await ws.send_json({"type": "error", "content": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001
                pass

    runner = asyncio.create_task(pump())
    try:
        while True:
            incoming = await ws.receive_json()
            if incoming.get("cmd") == "stop":
                cancel_token.cancel()
                await ws.send_json({"type": "stopped", "stop_reason": "Stopped by user."})
                return
    except WebSocketDisconnect:
        cancel_token.cancel()
        return
    finally:
        if handler is not None:
            logging.getLogger(EVENT_LOGGER_NAME).removeHandler(handler)
        runner.cancel()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse((WORKSPACE / "index.html").read_text(encoding="utf-8"))


@app.get("/api/context")
async def context() -> JSONResponse:
    """Sidebar metadata only — filenames/sizes/previews for the HUMAN.

    Previously this shipped file bodies. It is browser-only (never enters the LLM
    context), but sending full text was still wasteful, so it now returns
    summaries: name, byte size, mtime, and a one-line preview.
    """
    data: dict[str, Any] = {"project": str(PROJECT_ROOT), "files": [], "open_decisions": 0}
    for name in ("PORT_LOG_domain.md", "PORT_LOG_shell.md", "PORT_LOG_assets.md",
                 "OVERNIGHT_BLOCKERS.md", "DECISIONS.md", "BACKLOG.md"):
        p = PROJECT_ROOT / name
        if not p.exists():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            first = next((ln.strip() for ln in lines if ln.strip().startswith("#")), "")
            data["files"].append({
                "name": name,
                "lines": len(lines),
                "kb": round(p.stat().st_size / 1024, 1),
                "preview": first[:80],
            })
        except OSError:
            continue
    dec = PROJECT_ROOT / "DECISIONS.md"
    if dec.exists():
        data["open_decisions"] = sum(
            1 for ln in dec.read_text(encoding="utf-8", errors="replace").splitlines()
            if "Status: OPEN" in ln or "Status:  OPEN" in ln
        )
    return JSONResponse(data)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    try:
        cfg = await ws.receive_json()
        task = str(cfg.get("task", "")).strip()
        if not task:
            await ws.send_json({"type": "error", "content": "Empty task."})
            return
        allow_write = True
        max_rounds = int(cfg.get("max_turns", 80))
        trace = bool(cfg.get("trace", True))
        await ws.send_json({"type": "start", "allow_write": allow_write,
                            "max_rounds": max_rounds, "trace": trace})
        await run_agent_task(ws, task, allow_write, max_rounds, trace)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            await ws.send_json({"type": "error", "content": f"{type(exc).__name__}: {exc}"})
        except Exception:  # noqa: BLE001
            pass