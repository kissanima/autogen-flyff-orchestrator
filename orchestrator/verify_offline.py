"""Offline verification for the MAF rewrite. No API calls, no network.

    python orchestrator/verify_offline.py

Exits non-zero on the first failed group; prints OK/FAIL per check.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_framework import (  # noqa: E402
    AgentResponse,
    AgentResponseUpdate,
    BaseAgent,
    Content,
    Message,
)

from orchestrator import core, workflow  # noqa: E402

PASS, FAIL = 0, 0


def tool_text(result) -> str:
    """Tool results come back as a list of Content objects."""
    if isinstance(result, list):
        return " ".join(getattr(c, "text", None) or str(c) for c in result)
    return str(result)


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {label}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


class FakeAgent(BaseAgent):
    """Deterministic stand-in for a real Agent: no client, no network."""

    def __init__(self, name: str, replies: list[str]) -> None:
        super().__init__(name=name)
        self.replies = list(replies)
        self.calls = 0

    def run(self, messages=None, *, stream=False, session=None, **kwargs):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if stream:
            async def gen():
                yield AgentResponseUpdate(contents=[Content.from_text(reply)],
                                          role="assistant", author_name=self.name)
            return gen()

        async def coro():
            return AgentResponse(messages=[Message("assistant", [reply],
                                                   author_name=self.name)])

        return coro()


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="maf-verify-"))

    print("1) Config and paths")
    check("model separation", core.MODELS == {"domain": core.MODEL_PRO,
                                              "shell": core.MODEL_FLASH},
          str(core.MODELS))
    check("history buffer bounded", 0 < core.HISTORY_BUFFER <= 64,
          str(core.HISTORY_BUFFER))
    check("two write scopes", set(core.WRITE_SCOPES) == {"domain", "shell"})

    print("\n2) Scope enforcement (in the tools, not a stage)")
    check("domain -> Assets blocked",
          core.scope_violation('echo x > "Assets/Foo.cs"', "domain") is not None)
    check("domain -> Domain allowed",
          core.scope_violation('echo x > "Domain/Foo.cs"', "domain") is None)
    check("shell -> Domain blocked",
          core.scope_violation('echo x > "Domain/Foo.cs"', "shell") is not None)
    check("shell -> Assets allowed",
          core.scope_violation('echo x > "Assets/Foo.cs"', "shell") is None)
    check("the bug we found is unrepresentable: domain write refused even when shell "
          "spoke last", core.scope_violation('cp a.cs "Assets/b.cs"', "domain") is not None)

    print("\n3) Destructive-command screen")
    blocked = [
        "git push origin main",
        "git push",
        'git -c core.x=1 push',
        "git.exe push",
        'subprocess.run(["git", "push"])',
        'os.system("git " + "push")',
        "git reset --hard HEAD~3",
        "git clean -fdx",
        "rm -rf /",
        'Remove-Item -Recurse -Force "C:/Users"',
    ]
    for cmd in blocked:
        check(f"blocked: {cmd[:42]}", core.command_violation(cmd) is not None)
    allowed = ["git status", "git commit -m 'port dungeons'", "dotnet build Domain",
               "rm -rf obj/Debug", "git add Domain Tools"]
    for cmd in allowed:
        check(f"allowed: {cmd[:42]}", core.command_violation(cmd) is None)

    print("\n4) Scoped tools")
    core.PROJECT_ROOT = tmp  # redirect the project root for tool tests
    tools = {t.name: t for t in core.make_tools("domain")}
    check("tool set", set(tools) == {"read_file", "list_dir", "write_file",
                                     "run_shell", "log_progress"}, str(sorted(tools)))
    (tmp / "Domain").mkdir(exist_ok=True)
    (tmp / "Assets").mkdir(exist_ok=True)
    out = tool_text(await tools["write_file"].invoke(path="Domain/a.cs", content="class A {}"))
    check("in-scope write succeeds", "WROTE" in str(out), str(out)[:60])
    out = tool_text(await tools["write_file"].invoke(path="Assets/b.cs", content="class B {}"))
    check("cross-scope write refused", "REFUSED" in str(out), str(out)[:60])
    check("nothing was written outside scope", not (tmp / "Assets" / "b.cs").exists())
    out = tool_text(await tools["read_file"].invoke(path="Domain/a.cs"))
    check("read_file returns contents", "class A" in str(out))
    out = tool_text(await tools["run_shell"].invoke(command="git push origin main"))
    check("run_shell refuses push", "BLOCKED" in str(out), str(out)[:60])
    out = tool_text(await tools["run_shell"].invoke(command="echo hello"))
    check("run_shell executes", "hello" in str(out), str(out)[:40].replace("\n", " "))

    print("\n5) MCP server configuration (constructed, not connected)")
    saved = core.MCP_ENABLED
    try:
        core.MCP_ENABLED = "rea,unity"
        names = [t.name for t in core.make_mcp_tools()]
        check("both servers configured", names == ["rea", "unity"], str(names))
        core.MCP_ENABLED = "rea"
        check("MCP_SERVERS selects a subset",
              [t.name for t in core.make_mcp_tools()] == ["rea"])
        core.MCP_ENABLED = ""
        check("empty MCP_SERVERS disables MCP", core.make_mcp_tools() == [])
        core.MCP_ENABLED = "bogus"
        check("unknown server is ignored", core.make_mcp_tools() == [])
    finally:
        core.MCP_ENABLED = saved
    class _T:
        def __init__(self, n):
            self.name = n

    fake_mcp = [_T("rea"), _T("unity")]
    saved_routing = core.MCP_ROUTING
    try:
        core.MCP_ROUTING = "shared"
        check("shared routing gives both servers to both agents",
              [t.name for t in workflow.mcp_tools_for("domain", fake_mcp)] == ["rea", "unity"]
              and [t.name for t in workflow.mcp_tools_for("shell", fake_mcp)] == ["rea", "unity"])
        core.MCP_ROUTING = "split"
        check("split routing: domain gets rea only",
              [t.name for t in workflow.mcp_tools_for("domain", fake_mcp)] == ["rea"])
        check("split routing: shell gets unity only",
              [t.name for t in workflow.mcp_tools_for("shell", fake_mcp)] == ["unity"])
        check("split routing tolerates no MCP at all",
              workflow.mcp_tools_for("domain", None) == [])
    finally:
        core.MCP_ROUTING = saved_routing
    check("default routing preserves the old behaviour", core.MCP_ROUTING == "shared",
          core.MCP_ROUTING)
    check("rea uses npx", core.MCP_SERVERS["rea"]["command"] == "npx")
    check("request timeout is generous enough for npx cold start",
          core.MCP_TIMEOUT >= 30, str(core.MCP_TIMEOUT))

    print("\n6) Bounded context")
    msgs = [Message("user", ["task"])] + [Message("assistant", [f"m{i}"])
                                          for i in range(40)]
    bounded = workflow.bound_context(msgs, buffer=6)
    check("task message pinned first", bounded[0].text == "task")
    check("window bounded", len(bounded) == 7, str(len(bounded)))
    check("keeps the most recent", bounded[-1].text == "m39")

    print("\n7) Coordinator routing and stop conditions")
    from agent_framework import AgentExecutorResponse

    def resp(source: str, text: str) -> AgentExecutorResponse:
        return AgentExecutorResponse(
            executor_id=source,
            agent_response=AgentResponse(messages=[Message("assistant", [text],
                                                           author_name=source)]),
            full_conversation=[])

    class Ctx:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        async def send_message(self, message, target_id=None) -> None:
            self.sent.append((target_id, message.executor_id))

    budget = core.Budget(limit=10)
    coord = workflow.Coordinator(["domain", "shell"], budget, max_rounds=5)
    ctx = Ctx()
    await coord.on_response(resp("domain", "working"), ctx)
    check("domain response routed to shell", ctx.sent and ctx.sent[-1][0] == "shell",
          str(ctx.sent))
    await coord.on_response(resp("shell", "done"), ctx)
    check("shell response routed back to domain", ctx.sent[-1][0] == "domain")

    coord2 = workflow.Coordinator(["domain", "shell"], budget, max_rounds=5)
    ctx2 = Ctx()
    await coord2.on_response(resp("domain", f"{core.DONE_TOKEN}: all A items done"), ctx2)
    await coord2.on_response(resp("shell", f"{core.DONE_TOKEN}: all B items done"), ctx2)
    check("stops when both agents report done", coord2.state.reason == "complete",
          coord2.state.reason)
    check("no message sent after completion", len(ctx2.sent) == 1, str(ctx2.sent))

    coord3 = workflow.Coordinator(["domain", "shell"], core.Budget(limit=1), max_rounds=5)
    coord3.budget.exhausted = True
    await coord3.on_response(resp("domain", "x"), Ctx())
    check("stops on budget", coord3.state.reason == "budget", coord3.state.reason)

    coord4 = workflow.Coordinator(["domain", "shell"], core.Budget(limit=99), max_rounds=5,
                                  idle_limit=2)
    for _ in range(2):
        await coord4.on_response(resp("domain", core.WAIT_TOKEN), Ctx())
    check("stops when stalled", coord4.state.reason == "stalled", coord4.state.reason)

    coord5 = workflow.Coordinator(["domain", "shell"], core.Budget(limit=99), max_rounds=2)
    ctx5 = Ctx()
    await coord5.on_response(resp("domain", "a"), ctx5)
    await coord5.on_response(resp("shell", "b"), ctx5)
    await coord5.on_response(resp("domain", "c"), ctx5)
    check("stops at max_rounds", coord5.state.reason == "max_rounds", coord5.state.reason)

    print("\n8) Full workflow with fake agents")
    run_dir = tmp / "runs" / "test"
    fakes = {"domain": FakeAgent("domain", ["A work", core.DONE_TOKEN + ": A done"]),
             "shell": FakeAgent("shell", ["B work", core.DONE_TOKEN + ": B done"])}
    wf_budget = core.Budget(limit=100)
    wf, coordinator, storage = workflow.build_workflow(fakes, wf_budget, run_dir,
                                                       max_rounds=6)
    check("graph nodes",
          {e.id for e in wf.get_executors_list()} == {"seed", "domain", "shell",
                                                      "coordinator"},
          str(sorted(e.id for e in wf.get_executors_list())))

    events: list[dict] = []
    async for event in wf.run("do the task", stream=True):
        if getattr(event, "type", "") == "executor_completed":
            text = workflow._event_text(event.data)
            events.append({"source": event.executor_id, "text": text})

    check("both agents ran", {e["source"] for e in events} >= {"domain", "shell"},
          str([e["source"] for e in events]))
    check("converged on completion", coordinator.state.reason == "complete",
          coordinator.state.reason or "(none)")
    check("transcript captured", len(events) >= 4, str(len(events)))
    check("coordinator state persisted", (run_dir / "coordinator.json").exists())
    checkpoints = list((run_dir / "checkpoints").glob("**/*")) if (
        run_dir / "checkpoints").exists() else []
    check("checkpoints written", len(checkpoints) > 0, str(len(checkpoints)))

    print("\n9) Budget middleware")
    from agent_framework import ChatContext

    b = core.Budget(limit=2)
    mw = core.budget_middleware(b)
    calls = {"n": 0}

    async def call_next_ok() -> None:
        calls["n"] += 1

    ctx = ChatContext(client=None, messages=[], options={}, stream=False)  # type: ignore[arg-type]
    await mw(ctx, call_next_ok)
    await mw(ctx, call_next_ok)
    check("counts each attempt", b.used == 2, str(b.used))
    try:
        await mw(ctx, call_next_ok)
        check("refuses past the limit", False)
    except core.BudgetExhausted:
        check("refuses past the limit", b.exhausted)

    b2 = core.Budget(limit=10)
    mw2 = core.budget_middleware(b2)
    attempts = {"n": 0}

    async def flaky() -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("transient")

    import orchestrator.core as c
    orig = asyncio.sleep
    asyncio.sleep = lambda *_a, **_k: orig(0)  # no real waiting in tests
    try:
        await mw2(flaky.__self__ if False else ChatContext(client=None, messages=[], options={}, stream=False), flaky)  # type: ignore[arg-type]
    finally:
        asyncio.sleep = orig
    check("retries transient errors", attempts["n"] == 3, str(attempts["n"]))
    check("retries counted against budget", b2.used == 3, str(b2.used))

    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nOFFLINE VERIFICATION: {'PASSED' if FAIL == 0 else 'FAILED'} "
          f"({PASS} passed, {FAIL} failed)")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
