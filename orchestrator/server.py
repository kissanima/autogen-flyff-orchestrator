"""FastAPI + WebSocket front end for the MAF orchestrator.

    python -m orchestrator.server
    # then open http://127.0.0.1:8765
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from . import core, workflow

app = FastAPI(title="FlyFF MAF Orchestrator")

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"

_run_task: asyncio.Task | None = None


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(INDEX)


@app.get("/api/config")
async def config() -> dict[str, Any]:
    return {
        "models": core.MODELS,
        "scopes": {k: list(v) for k, v in core.WRITE_SCOPES.items()},
        "project_root": str(core.PROJECT_ROOT),
        "history_buffer": core.HISTORY_BUFFER,
        "max_requests": core.MAX_REQUESTS,
        "max_rounds": core.MAX_ROUNDS,
        "max_hours": core.MAX_HOURS,
        "kickoff": workflow.KICKOFF,
        "framework": "Microsoft Agent Framework",
    }


@app.get("/api/context")
async def context() -> dict[str, Any]:
    """Summaries only: file name, size, line count, short preview.

    Never sends file bodies to the model - this endpoint feeds the sidebar only.
    """
    files: list[dict[str, Any]] = []
    root = core.PROJECT_ROOT
    if not root.exists():
        return {"project_root": str(root), "exists": False, "files": []}
    for path in sorted(root.rglob("*")):
        if len(files) >= 40:
            break
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in core.BLOCKED_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() not in {".cs", ".md", ".json", ".txt", ".asmdef"}:
            continue
        try:
            size = path.stat().st_size
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.count("\n") + 1
        files.append({"name": str(rel), "kb": round(size / 1024, 1), "lines": lines,
                      "preview": " ".join(text[:120].split())})
    return {"project_root": str(root), "exists": True, "files": files,
            "truncated": len(files) >= 40}


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    global _run_task
    await ws.accept()
    send_lock = asyncio.Lock()

    async def send(payload: dict[str, Any]) -> None:
        async with send_lock:
            try:
                await ws.send_json(payload)
            except (RuntimeError, WebSocketDisconnect):
                raise WebSocketDisconnect()

    try:
        while True:
            incoming = await ws.receive_json()
            command = incoming.get("cmd", "run")

            if command == "stop":
                if _run_task and not _run_task.done():
                    _run_task.cancel()
                await send({"type": "stopped", "stop_reason": "Stopped by user."})
                continue

            if command != "run":
                await send({"type": "error", "content": f"unknown command {command!r}"})
                continue

            task_text = (incoming.get("task") or workflow.KICKOFF).strip()
            max_rounds = int(incoming.get("max_rounds") or core.MAX_ROUNDS)
            max_hours = float(incoming.get("max_hours") or core.MAX_HOURS)
            run_dir = core.RUNS_DIR / time.strftime("%Y%m%d-%H%M%S")
            budget = core.Budget(limit=core.MAX_REQUESTS, path=run_dir / "budget.json")

            await send({"type": "start", "run_id": run_dir.name,
                        "max_rounds": max_rounds, "max_hours": max_hours,
                        "framework": "MAF"})

            def emit(payload: dict[str, Any]) -> None:
                if _run_task is None or _run_task.done():
                    return
                asyncio.get_running_loop().create_task(send(payload))

            async def runner() -> None:
                try:
                    summary = await workflow.run_workflow(
                        task_text, run_dir=run_dir, budget=budget,
                        on_event=emit, max_rounds=max_rounds, max_hours=max_hours,
                        resume=bool(incoming.get("resume")))
                    await send({"type": "done",
                                "stop_reason": f"{summary['reason']}: {summary['detail']}",
                                "summary": summary})
                except asyncio.CancelledError:
                    await send({"type": "stopped", "stop_reason": "Stopped by user."})
                    raise
                except Exception as exc:  # noqa: BLE001
                    await send({"type": "error",
                                "content": f"{type(exc).__name__}: {exc}"})

            _run_task = asyncio.create_task(runner())
            await _run_task

    except WebSocketDisconnect:
        if _run_task and not _run_task.done():
            _run_task.cancel()
        return


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
