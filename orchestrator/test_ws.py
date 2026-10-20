"""End-to-end WebSocket test with fake agents. No API calls.

    python orchestrator/test_ws.py

Proves the UI path works: run command -> workflow -> bubbles -> done.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from orchestrator import core, server, workflow  # noqa: E402
from orchestrator.verify_offline import FakeAgent  # noqa: E402

# Fake agents instead of real ones: zero requests.
workflow.build_agents = lambda budget, on_event=None: {
    "domain": FakeAgent("domain", ["A: ported Dungeons", core.DONE_TOKEN + ": A done"]),
    "shell": FakeAgent("shell", ["B: built UI controllers", core.DONE_TOKEN + ": B done"]),
}
core.resolve_api_key = lambda: "unused"

client = TestClient(server.app)
received: list[dict] = []

with client.websocket_connect("/ws") as ws:
    ws.send_json({"cmd": "run", "task": "do the thing", "max_rounds": 6})
    for _ in range(40):
        payload = ws.receive_json()
        received.append(payload)
        if payload.get("type") in ("done", "stopped", "error"):
            break

kinds = [p.get("type") for p in received]
sources = [p.get("source") for p in received if p.get("type") == "message"]

print("event types:", kinds)
print("message sources:", sources)

ok = True
if "start" not in kinds:
    print("FAIL: no start event"); ok = False
if "done" not in kinds:
    print("FAIL: no done event"); ok = False
if not {"domain", "shell"} <= set(sources):
    print("FAIL: both agents did not produce bubbles"); ok = False
if kinds[-1] != "done":
    print("FAIL: run did not end with done"); ok = False

done = next((p for p in received if p.get("type") == "done"), {})
print("stop_reason:", done.get("stop_reason"))
summary = done.get("summary") or {}
print("summary:", {k: summary.get(k) for k in ("reason", "rounds", "done", "requests_used")})

print("\nWEBSOCKET TEST:", "PASSED" if ok else "FAILED")
raise SystemExit(0 if ok else 1)
