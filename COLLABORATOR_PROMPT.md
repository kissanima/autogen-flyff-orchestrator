# Collaborator Prompt — FlyFF MAF Orchestrator

> Give this to a second agent working **in parallel** with the primary agent.
> It is a peer contract, not a takeover briefing. The takeover version is
> `AI_HANDOFF_PROMPT.md` — read that first if you are the only agent.

---

## 1. Who you are

You are a **peer agent** on an active rewrite. Another agent (the primary) is working in the same
repository at the same time. You are not inheriting the project; you are taking one work package
inside it while it takes another.

Two rules follow from that, and they matter more than anything else here:

1. **You own a small set of files. Stay inside it.** Parallel agents that edit the same file
   produce conflicts that cost more than the work saved.
2. **Do not merge to `main`.** The primary decides when the branch is ready.

---

## 2. The project in one paragraph

An autonomous overnight coding orchestrator built on **Microsoft Agent Framework 1.22**. Two LLM
agents (`domain`, `shell`) work on disjoint directories of a large C++ → C#/Unity port, while a
deterministic `coordinator` routes their turns. Scope is enforced inside the tools, not in a stage.
This repository is the orchestrator; the agents edit a separate Unity project at
`FLYFF_PROJECT_ROOT` (default `D:\Flyff source code`).

---

## 3. Current state

| Branch | Contents | Status |
|---|---|---|
| `main` | The old AutoGen 0.7.5 implementation (`autogen_gui/`) | Frozen, published, **do not touch** |
| `maf-rewrite` | The MAF rewrite (`orchestrator/`) | Active. `verify_offline.py` 50/50, `test_ws.py` green, **no live model run yet** |

Work happens on `maf-rewrite` or on branches cut from it.

---

## 4. File ownership

**You own (create and edit freely):**

- `orchestrator/overnight.py` — the headless runner (new file)
- `orchestrator/test_overnight.py` — its offline test (new file)

**The primary owns (do not edit):**

- `orchestrator/core.py` — config, scoped tools, destructive screen, budget middleware
- `orchestrator/workflow.py` — prompts, agents, coordinator, graph
- `orchestrator/verify_offline.py`

**Shared — read freely, edit only after agreement:**

- `orchestrator/server.py`, `orchestrator/index.html`, `README.md`, `install.ps1`,
  `requirements.txt`, `PORT_LOG.md`

If you believe a shared or primary-owned file must change, **do not change it**. Describe the
change in a message: file, function, the diff you propose, and why your package needs it. The
primary applies it, so the edit has exactly one author.

---

## 5. Shared contracts — do not break these

These are the interfaces your package calls. Treat them as frozen.

```python
# orchestrator/core.py
Budget(limit: int = MAX_REQUESTS, used: int = 0, path: Path | None = None)
    .spend(n=1) -> None          # raises BudgetExhausted past the limit
    .used, .limit, .exhausted, .fatal, .remaining
class BudgetExhausted(Exception)
class FatalModelError(Exception)
MAX_REQUESTS: int                # default 4800 -- the real overnight limit
MAX_HOURS: float                 # default 8.0 -- wall clock; 0 disables
MAX_ROUNDS: int                  # default 0 == unlimited (runaway-loop guard only)
RUNS_DIR: Path                   # orchestrator/runs
resolve_api_key() -> str         # raises RuntimeError when unset

# orchestrator/workflow.py
KICKOFF: str
build_agents(budget, on_event=None, mcp_tools=None) -> dict[str, Agent]
build_workflow(agents, budget, run_dir, max_rounds=MAX_ROUNDS, max_hours=MAX_HOURS)
    -> (workflow, coordinator, storage)
async run_workflow(task, *, run_dir, budget, on_event,
                   max_rounds=MAX_ROUNDS, max_hours=MAX_HOURS, resume=False) -> dict
    # dict keys: reason, detail, rounds, done, requests_used, requests_limit,
    #            deadline_hours, notes
```

`run_workflow` is the only entry point you need. It returns a summary dict with `reason` in
`{complete, budget, fatal, deadline, max_rounds, stalled, stopped}`. Do not add a round cap to
your runner — pass `max_hours` and let the request budget and the clock do the bounding.

---

## 6. Your work package

**Port the headless runner.** The AutoGen version had `overnight.py`; the rewrite lost it. Build it
on MAF, in `orchestrator/overnight.py`, as a module runnable with `python -m orchestrator.overnight`.

Required behavior:

1. **CLI:** `--task`, `--task-file`, `--max-requests`, `--max-rounds`, `--resume [RUN_ID]`,
   `--allow-dirty`, `--allow-no-git`, `--no-branch`, `--idle-limit`.
2. **Preflight:** refuse to start on a dirty git tree in the target repo (unless `--allow-dirty`),
   refuse a non-git target (unless `--allow-no-git`), and create an `overnight/<timestamp>` branch
   (unless `--no-branch` or `--resume`).
3. **Run:** call `run_workflow` with a fresh `Budget` persisted at `RUNS_DIR/<id>/budget.json`;
   print one line per agent message; write `summary.json`.
4. **Stop:** create `RUNS_DIR/<id>/STOP` → stop cleanly; Ctrl+C → stop and keep the checkpoint.
5. **Keep Windows awake** for the duration (`SetThreadExecutionState`), and always release it in a
   `finally`.
6. **Exit code** 0 on `complete`, 1 otherwise.

Non-goals: no UI, no MCP, no changes to the workflow, no new dependencies.

**Write `orchestrator/test_overnight.py`** that proves all six points with fake agents and no API
calls, in the same style as `verify_offline.py` (`check(label, ok, detail)` + a PASSED/FAILED
summary). It must run in under 10 seconds and must not touch `D:\Flyff source code` — use a
temporary directory and monkeypatch `core.PROJECT_ROOT` / `core.RUNS_DIR`.

---

## 7. How we coordinate

- **Branch:** cut `feature/overnight` from `maf-rewrite`. Do not commit directly to `maf-rewrite`.
- **Commits:** atomic, explicit paths, no `git add -A`, no build artifacts, no `.env`.
- **Never** force-push, rebase, or reset a branch you do not own.
- **Never** run the agents against the real model. A live run costs real requests and can take
  hours; the primary schedules those.
- **Report format** when you finish, in this order: what you built, the exact commands you ran and
  their output, what you could not verify, what you need from the primary.

---

## 8. Verification bar

Nothing is "done" until both of these pass on your branch:

```powershell
$env:DEEPSEEK_API_KEY="sk-offline-verify"     # dummy; no API calls are made
python orchestrator\verify_offline.py         # 50 checks, must stay at 50/50
python orchestrator\test_ws.py                # end-to-end WebSocket, must pass
python orchestrator\test_overnight.py         # yours
```

If `verify_offline.py` drops below 50, you broke a shared contract — revert, do not "fix" the test.

On this machine the interpreter is
`C:\Users\Admin\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe`. There is no `venv/`
in the repo.

---

## 9. MAF landmines already paid for

Do not rediscover these:

- A custom agent subclasses `BaseAgent` and implements `def run(self, messages=None, *,
  stream=False, session=None, **kw)` as a **plain def** returning an awaitable or an async
  generator. `async def` breaks streaming.
- Edge typing is validated at `build()`: an `AgentExecutor` emits `AgentExecutorResponse`, so a
  downstream node annotated `Message` raises `TypeCompatibilityError`.
- `@executor` functions take `(input, ctx: WorkflowContext[Out])`. `ctx.send_message(msg,
  target_id=...)` routes; returning without sending converges the workflow.
- `WorkflowBuilder(max_iterations=N)` raises `WorkflowConvergenceException` if the graph never
  converges.
- Tool `.invoke(...)` returns a **list of `Content`** — use `content.text`.
- Chat middleware is `async def mw(ctx: ChatContext, call_next) -> None`; the result lands in
  `ctx.result`.

---

## 10. Escalation

Stop and ask the primary — do not guess — if:

- the work needs a change to a primary-owned or shared file,
- a shared contract in section 5 looks wrong,
- you cannot make a test pass without weakening it,
- you are about to run anything that costs API requests.
