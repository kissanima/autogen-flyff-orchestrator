"""E2E WebSocket test for the AutoGen overnight-run backend.

Small bounded run (max_rounds=6, writes off) so it stays cheap.
"""

import asyncio
import json

import websockets

URI = "ws://127.0.0.1:8765/ws"

TASK = (
    "Welcome to the overnight run. You have a 5,000 request budget. \n"
    "Agent A: Use list_dir to show the top-level entries of the project, then read "
    "BACKLOG.md and confirm you own A1 (Dungeons). Do not start coding in this "
    "smoke test - just report what you see.\n"
    "Agent B: After Agent A, read OVERNIGHT_BLOCKERS.md (max_lines 40) and summarise "
    "blocker 1 in one sentence. Do not start building in this smoke test.\n"
    "Supervisor: nothing to verify in this smoke test - stay silent."
)


async def main() -> None:
    seen = {"tool_req": 0, "tool_res": 0, "text": 0, "trace": 0, "sources": set(),
            "done": None, "next_noise": 0}
    async with websockets.connect(URI, max_size=4 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "task": TASK,
            "allow_write": False,
            "max_turns": 6,
            "trace": True,
        }))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=240))
            t = m.get("type")
            if t == "start":
                print(f"[start] writes={m['allow_write']} max_rounds={m['max_rounds']} trace={m['trace']}")
            elif t == "ToolCallRequestEvent":
                seen["tool_req"] += 1
                for c in m.get("tool_calls", []):
                    print(f"[tool call]  [{m['source']}] {c['name']}({c['arguments'][:70]})")
            elif t == "ToolCallExecutionEvent":
                seen["tool_res"] += 1
                print(f"[tool result] [{m['source']}] {m['content'][:80]}...")
            elif t in ("TextMessage", "UserInputMessage"):
                seen["text"] += 1
                seen["sources"].add(m["source"])
                if m["source"] == "supervisor" and m["content"].strip() in ("", "NEXT"):
                    seen["next_noise"] += 1
                print(f"[{m['source']}] {m['content'][:100]}...")
            elif t == "trace":
                seen["trace"] += 1
            elif t == "done":
                seen["done"] = m["stop_reason"]
                print(f"[done] {m['stop_reason']}")
                break
            elif t == "error":
                print(f"[ERROR] {m['content']}")
                break

    print("\n=== SUMMARY ===")
    print(f"tool calls requested: {seen['tool_req']}")
    print(f"tool results        : {seen['tool_res']}")
    print(f"text messages       : {seen['text']}  sources={sorted(seen['sources'])}")
    print(f"NEXT/noise messages : {seen['next_noise']} (must be 0)")
    print(f"trace events        : {seen['trace']}")
    print(f"stop reason         : {seen['done']}")
    ok = seen["tool_req"] >= 1 and seen["done"] is not None and seen["trace"] > 0 \
        and seen["next_noise"] == 0
    print("E2E TEST:", "PASSED" if ok else "FAILED")


if __name__ == "__main__":
    asyncio.run(main())