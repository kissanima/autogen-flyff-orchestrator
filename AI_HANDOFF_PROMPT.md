# AI Handoff Prompt — FlyFF MAF Orchestrator

> Paste this path at the start of a session with any capable coding agent.
> Written as a direct briefing. Read it fully before touching code.

---

## 1. Your role

You are taking over a working project that was just rewritten. The AutoGen implementation still
exists on `main`; the current architecture lives on the `maf-rewrite` branch (and on `main` once it
is promoted). Do not re-derive the design. Do not reintroduce group-chat patterns.

---

## 2. What the project is

An autonomous, overnight coding orchestrator on **Microsoft Agent Framework 1.22**. Two LLM agents
work on disjoint directories of a large C++ → C#/Unity port (FlyFF → Unity 6 / FishNet), while a
deterministic coordinator routes turns and tools enforce directory ownership.

**This repo is the orchestrator.** The agents edit a different repository (default
`D:\Flyff source code`, override `FLYFF_PROJECT_ROOT`).

---

## 3. Hard facts

| Item | Value |
|---|---|
| Framework | `agent-framework>=1.22,<2` (Microsoft Agent Framework, GA April 2026) |
| Entry | `python -m orchestrator.server` → <http://127.0.0.1:8765> |
| Models | `domain` = `deepseek-v4-pro`, `shell` = `deepseek-v4.1-flash` |
| Client | `agent_framework.openai.OpenAIChatCompletionClient(base_url=...)` |
| Secrets | `.env` only; `core.resolve_api_key()` raises when missing |
| Tests | `orchestrator/verify_offline.py` (50 checks), `orchestrator/test_ws.py` — no API calls |

---

## 4. Architecture

```
seed(task) → domain → coordinator → shell → coordinator → domain → ...
```

- **seed** — `@executor`, turns the task string into a `Message`.
- **domain / shell** — `AgentExecutor(agent, context_mode="custom", context_filter=bound_context)`.
- **coordinator** — a class-based `Executor` with `@handler`. No model. Routes the
  `AgentExecutorResponse` to the other agent, or returns without sending, which converges the
  workflow.

`AgentExecutorResponse` carries `full_conversation` with `author_name` preserved. That is how a
node can attribute a message to its author — the capability AutoGen's `CodeExecutorAgent` lacked,
and the root of the old gate bug.

---

## 5. Invariants — do not break

1. **Scope is enforced in the tools**, not in a stage. `write_file` refuses out-of-scope paths;
   `run_shell` screens redirects and `cp`/`mv` destinations. Never add a "gate" participant back —
   a stage cannot un-queue work, which is exactly why the old design failed.
2. **`[WAIT]` is not needed here.** If you find yourself adding an idle-token optimisation, you
   have probably reintroduced a broadcast.
3. **Context stays bounded.** `bound_context` keeps the task message plus the last
   `HISTORY_BUFFER` messages. Cost must not grow with turn count.
4. **Every request is counted.** `budget_middleware` counts each attempt, retries included. Never
   create a client that bypasses it.
5. **`git push` stays blocked.** Detection is token-based on purpose; `subprocess.run(["git",
   "push"])` must remain caught. If you tighten it, test the list form.
6. **Secrets never enter source.** `.env` only.
7. **Append to `PORT_LOG.md` at the end of every session** (Status / What was done / Next Steps /
   Blockers, under 20 lines).

---

## 6. MAF landmines (verified against 1.22.0)

- A custom agent subclasses `BaseAgent` and implements
  `def run(self, messages=None, *, stream=False, session=None, **kw)`. It must be a **plain def**
  returning either an awaitable (`AgentResponse`) or an async generator of `AgentResponseUpdate` —
  an `async def` returns a coroutine and breaks streaming with
  `'async for' requires __aiter__`.
- Workflow type validation is strict: an `AgentExecutor` emits `AgentExecutorResponse`, so a
  downstream `@executor(output=Message)` fails at `build()` with `TypeCompatibilityError`.
- `@executor` functions take `(input, ctx: WorkflowContext[Out])`. `ctx.send_message(msg,
  target_id=...)` routes explicitly; **returning without sending converges the workflow**.
- `WorkflowBuilder(max_iterations=N)` raises `WorkflowConvergenceException` if the graph never
  converges. Set it to roughly `4 × max_rounds + 8`.
- Tools are `@tool(name=..., description=...)` on typed functions. `.invoke(...)` returns a **list
  of `Content`**, not a string — use `content.text`.
- Chat middleware is `async def mw(ctx: ChatContext, call_next) -> None`; the result lands in
  `ctx.result`. This is where budget counting and retry live.
- `FileCheckpointStorage(path)` passed to `WorkflowBuilder(checkpoint_storage=...)`; resume with
  `workflow.run(task, checkpoint_id=...)`.
- `Agent` is stateless by default. History comes from `AgentExecutor`'s conversation cache plus
  `context_filter`, not from the agent itself.

---

## 7. Run and verify

```powershell
python -m orchestrator.server          # UI on 8765

$env:DEEPSEEK_API_KEY="sk-offline-verify"   # dummy; these make no API calls
python orchestrator\verify_offline.py       # 50 checks
python orchestrator\test_ws.py              # WebSocket end-to-end with fake agents
```

Live runs cost real requests. Never use one to test a code change.

On this machine the working interpreter is
`C:\Users\Admin\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe` (MAF 1.22 installed
there). There is no `venv/` in the repo.

---

## 8. Known gaps

- **MCP is not wired.** The AutoGen version attached `rea` (170 tools) and `unity` (160 tools) via
  `McpWorkbench`. MAF has `MCPStdioTool(name, command, args=..., request_timeout=...)`, which is a
  drop-in, but it is not connected yet. Add it to `core.make_tools` or to `Agent(tools=[...])`, and
  remember MCP calls are **not** scope-checked.
- **No headless runner.** The old `overnight.py` (git preflight, keep-awake, `--resume`) was not
  ported. `run_workflow(resume=True)` supports checkpoint resume; the CLI wrapper is missing.
- **Resume is partial.** `Coordinator` persists `rounds` and `done` to `coordinator.json`, but the
  `done` flags are only restored on a fresh `Coordinator`, and the checkpoint id must be supplied.
- **No live streaming to the UI.** Bubbles are emitted per `executor_completed`, not per token.
- **Token-level `git push` detection is broad.** A commit message containing the words "git" and
  "push" will be refused. Acceptable for unattended runs; revisit if it bites.

---

## 9. Rules of engagement

- Verify, do not guess. Introspect the installed package before using an API you have not confirmed.
- Do not run agents to test code. Use the offline suites.
- Stage explicitly. Never `git add -A`. Never commit `.env`, `venv/`, `__pycache__/`, `runs/`.
- Ask before changing the topology (new participant, new stop condition, different model split).
- Record the outcome in `PORT_LOG.md`.
