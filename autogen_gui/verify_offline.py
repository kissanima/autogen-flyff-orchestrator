"""Offline verification of the final team configuration. No LLM calls."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from autogen_agentchat.messages import TextMessage  # noqa: E402
from autogen_core import CancellationToken  # noqa: E402

import server  # noqa: E402

ok = True


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok
    print(f"  {'OK  ' if cond else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    ok = ok and cond


async def main() -> int:
    print("1) MCP params")
    for name, p in server.MCP_SERVERS.items():
        check(f"{name} read_timeout_seconds == 60",
              p.read_timeout_seconds == 60, str(p.read_timeout_seconds))

    print("\n2) Team composition")
    team = server.build_team(allow_write=True, max_rounds=80)
    names = [a.name for a in team._participants]
    check("participants", names == ["domain", "shell", "gate", "executor"], str(names))
    wrappers = team._participants[:2]
    inner = [w._agent for w in wrappers]
    check("both agents use a workbench (not tools=)",
          all(isinstance(a._workbench, list) and a._workbench for a in inner))
    check("both agents have all MCP servers attached",
          all(set(a._workbench[0]._mcp) == {"rea", "unity"} for a in inner))
    check("both agents have local tools in the composite",
          all(len(a._workbench[0]._local._tools) >= 3 for a in inner))
    check("no Supervisor references in prompts",
          "Supervisor" not in server.DOMAIN_SYSTEM + server.SHELL_SYSTEM)
    for label, prompt in (("DOMAIN", server.DOMAIN_SYSTEM), ("SHELL", server.SHELL_SYSTEM)):
        check(f"{label} prompt has [WAIT] + OVERNIGHT_COMPLETE",
              "[WAIT]" in prompt and "OVERNIGHT_COMPLETE" in prompt)
        check(f"{label} prompt mentions Executor",
              "Executor" in prompt and "Supervisor" not in prompt)

    print("\n3) Scope enforcement (gate)")
    check("domain -> Assets blocked",
          server.scope_violation('echo x > "Assets/Foo.cs"', "domain") is not None)
    check("shell -> Domain blocked",
          server.scope_violation('cp a.cs "Domain/b.cs"', "shell") is not None)
    check("domain -> Domain allowed",
          server.scope_violation('echo x > "Domain/Foo.cs"', "domain") is None)
    check("shell -> Assets allowed",
          server.scope_violation('echo x > "Assets/Foo.cs"', "shell") is None)

    print("\n4) Gate behaviour")
    gate = team._participants[2]
    tok = CancellationToken()
    r_block = await gate.on_messages(
        [TextMessage(source="domain",
                     content='I will write\n```sh\necho hi > "Assets/x.cs"\n```')], tok)
    check("cross-scope write blocked", r_block.chat_message.content.startswith("BLOCKED"))
    r_pass = await gate.on_messages(
        [TextMessage(source="domain", content='```sh\necho hi > "Domain/x.cs"\n```')], tok)
    check("in-scope write passes silently", r_pass.chat_message.content == "")

    print("\n5) [WAIT] economics")
    waitable = team._participants[0] if team._participants[0].name == "domain" else None
    assert waitable is not None
    # Waiting state: last domain turn was [WAIT] and shell has said nothing since.
    hist = [
        TextMessage(source="user", content="start"),
        TextMessage(source="domain", content="I ported Dungeons."),
        TextMessage(source="shell", content="[WAIT]"),
        TextMessage(source="domain", content="[WAIT]"),
    ]
    waitable._waiting = True
    r = await waitable.on_messages(hist, tok)
    check("waiting agent returns [WAIT] without an LLM call",
          r.chat_message.content == "[WAIT]")
    # Reactivation: shell produced real work after our last turn.
    hist2 = hist + [TextMessage(source="shell", content="I built the UI controllers.")]
    await waitable.on_messages(hist2, tok)
    check("reactivates when the other agent does work", waitable._waiting is False)

    print("\n6) Incremental executor")
    ex = team._participants[3]
    msgs = [
        TextMessage(source="domain", content='```sh\necho OLD\n```'),
        TextMessage(source="executor", content="OLD output"),
        TextMessage(source="shell", content='```sh\necho NEW\n```'),
    ]
    blocks = await ex.extract_code_blocks_from_messages(msgs)
    check("only new blocks after last executor turn",
          len(blocks) == 1 and "NEW" in blocks[0].code,
          f"{[b.code.strip() for b in blocks]}")

    print("\n7) Executor + denylist")
    from autogen_core.code_executor import CodeBlock
    res = await ex._code_executor.execute_code_blocks(
        [CodeBlock(code="import os; print(os.getcwd())", language="python")], tok)
    check("executor cwd is project root", "Flyff source code" in res.output)
    res3 = await ex._code_executor.execute_code_blocks(
        [CodeBlock(code='import os; os.system("rm -rf /")', language="python")], tok)
    check("denylist blocks destructive code", res3.output.startswith("BLOCKED"))

    print("\n8) Dead code removed")
    src = Path("server.py").read_text(encoding="utf-8")
    check("run_shell gone", "def run_shell" not in src)
    check("no unused shutil import", "import shutil" not in src)
    check("no SmartSupervisor left", "SmartSupervisor" not in src)

    print("\n9) Context leak fixes")
    for label, prompt in (("DOMAIN", server.DOMAIN_SYSTEM), ("SHELL", server.SHELL_SYSTEM)):
        check(f"{label} no longer forces a turn-1 read",
              "Read `BACKLOG.md`" not in prompt and "first." not in prompt.split("\n\n")[0]
              or "If you need to know" in prompt)
        check(f"{label} uses the softer phrasing",
              "If you need to know the current task or rules" in prompt)
    check("history buffer is bounded",
          isinstance(server.HISTORY_BUFFER, int) and server.HISTORY_BUFFER <= 20,
          str(server.HISTORY_BUFFER))
    # Sidebar endpoint must not ship file bodies.
    import json as _json
    from fastapi.testclient import TestClient
    try:
        client = TestClient(server.app)
        raw = client.get("/api/context").text
        data = _json.loads(raw)
        check("context endpoint returns summaries, not bodies",
              "files" in data and "logs" not in data)
        check("context payload is small", len(raw) < 4000, f"{len(raw)} bytes")
    except Exception as exc:  # noqa: BLE001
        check("context endpoint reachable", False, f"{type(exc).__name__}")
    # Fresh session isolation.
    t2 = server.build_team(allow_write=True, max_rounds=80)
    check("new session has a fresh context object",
          t2._participants[0]._agent.model_context is not
          team._participants[0]._agent.model_context)

    print(f"\nOFFLINE VERIFICATION: {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))