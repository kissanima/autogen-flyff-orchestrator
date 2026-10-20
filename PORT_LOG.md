# PORT_LOG

## 2026-10-19 — AutoGen setup + MCP config

- **Status:** DONE — "Full Autonomy Enabled". Executor architecture built + offline-verified. NO agent runs since user's STOP.
- **What was done:** Removed SmartSupervisor. Team = [domain(pro), shell(flash), executor]. Executor = CodeExecutorAgent + SafeLocalExecutor(work_dir=project root, timeout 600) executing ```python/```sh blocks from agent messages (0.7.5 equivalent of UserProxyAgent code_execution_config). Agent prompts state terminal-via-executor verbatim + MUST dotnet test + MUST git commit + anti-loop. max_rounds default 80. Pump filters executor noise. verify_offline.py PASSED (executor runs python+sh in project root; denylist blocks destructive code).
- **Next Steps:** AWAIT user go-ahead. On go: start server (`uvicorn server:app --port 8765`, currently running bg), open http://127.0.0.1:8765, Run. Do NOT run test_e2e.py without user approval (burns agent requests).
- **Blockers:** None. Repo's Domain tests fail with pre-existing compile errors in ScriptInterpreter.cs (other agents' mid-work state).

## Unity MCP — RESOLVED

- **Status:** DONE. Both MCP servers verified working, including through AutoGen.
- **Root cause of "0 tools":** the `unity mcp` CLI is only a proxy — it needs `com.unity.pipeline` in the project + a running Editor. The project lacked the package entirely.
- **Fix:** `unity pipeline install --project-path "D:/Flyff source code"` (installed 0.8.0-exp.1), then restart the Editor.
- **Headless start:** `Unity.exe -batchmode -project-path "D:/Flyff source code" -nographics -logFile <log>`; confirm with `unity status` → `7800 ready`.
- **Verified:** unity = 160 MCP tools (set_selection executed → returned JSON); rea = 170 tools; AutoGen `McpWorkbench` sees all 160/170.
- **Next Steps:** To use MCP inside the AutoGen team, add McpWorkbench with `read_timeout_seconds=60..90` (default 5s breaks cold start) and extend the Shell agent's `AssistantAgent` with `workbench=...`. Not wired yet — awaiting instruction.
## Final team config — MCP wired, [WAIT], scope gate

- **Status:** DONE — "System Ready for Overnight Run". `verify_offline.py` 25/25 PASSED (offline, no LLM calls). Server up on 8765; Unity editor `ready` on 7800.
- **What was done:** Both agents now carry BOTH MCP servers (rea + unity, `read_timeout_seconds=60`) merged with their local file tools via `CompositeWorkbench` (AutoGen forbids `tools=` + `workbench=`). `[WAIT]` economics via WaitableAgent wrappers (zero API calls while waiting). New `gate` participant blocks cross-scope writes before the executor sees them (executor can't attribute code-block authors). `IncrementalExecutor` stops re-running historical code blocks every cycle. Prompts: Supervisor→Executor + verbatim [WAIT]/OVERNIGHT_COMPLETE rule. Dead code removed.
- **Next Steps:** Await the user's final task prompt, then Run from the UI.
- **Blockers:** None. Gotchas recorded in `.workbuddy-ai/memory/2026-10-19.md` (ToolSchema is a TypedDict; Workbench/ChatAgent ABC method lists; no manager injection in 0.7.5).

## Context leak fixed

- **Status:** DONE — "Context Leak Fixed". verify_offline.py 33/33 PASSED.
- **Diagnosis correction:** `/api/context` never entered the LLM context (browser-only). Real sinks were (a) prompts forcing a turn-1 read of BACKLOG/DECISIONS/PORT_LOG and (b) unbounded history replay per turn.
- **What changed:** prompts softened to "If you need to know… use your tools" + "do not re-read every turn"; both agents now use `BufferedChatCompletionContext(buffer_size=12)`; `/api/context` returns filename/line-count/KB/preview summaries only (624 bytes); session isolation verified fresh per run.
- **Next Steps:** Await final task prompt. Monitor per-turn token usage via the UI debug trace.
- **Blockers:** None. `PORT_LOG_domain.md` is 91.6 KB — never let agents read it whole.

## Packaging & publication

- **Status:** LOCAL REPO READY & SECURED. Awaiting GitHub private repo URL to push.
- **What was done:** Secrets moved to `.env` only (dotenv); removed the models.json fallback; added `.env.example`, `.gitignore`; `requirements.txt`; `install.ps1` (Python/Node bootstrap, venv, pip, .env setup, launch); `README.md` (features, install, usage, ASCII architecture, security); "📋 Copy Chat" button with timestamped export. Server now importable as `autogen_gui.server`. verify_offline.py 33/33 PASSED.
- **Security verification:** 0 secret matches in staged diff; `.env` untracked; no key in any tracked file.
- **Next Steps:** `git remote add origin <PRIVATE_URL>` then `git push -u origin main`.
- **Blockers:** Waiting on the repo URL.

## Published to GitHub (private)

- **Status:** DONE — "Setup Packaged, Secured, & Published".
- **Remote:** `origin` = `https://github.com/kissanima/autogen-flyff-orchestrator.git`. `main` local == `origin/main` (in sync, nothing ahead/behind).
- **Auth:** no `gh` CLI installed. Git Credential Manager (`credential.helper=manager`) already holds a working credential — `git ls-remote` succeeds non-interactively. No PAT needed.
- **Re-verified after push:** 15 tracked files, `.env` NOT tracked, secret scan over tracked tree = 0 matches.
- **Next Steps:** Await user's overnight task prompt; start server with `python -m uvicorn autogen_gui.server:app --port 8765`.
- **Blockers:** None.

## Review: Claude's unattended-mode patch

- **Status:** DONE (review only) — patch NOT applied to this repo.
- **Source:** `C:\Users\Admin\WorkBuddy AI\Claude patch\` (`unattended-mode.patch` + `files.zip`, extracted to `Claude patch/review/`).
- **Verdict:** merge it. +638 lines `autonomy.py`, +143 `overnight.py`, +195 in `server.py`, +443-line test suite. Both suites pass offline (`verify_offline` 39 checks, `verify_unattended` 6 groups). No secrets, no network calls, no eval/exec.
- **Real bug it fixes (reproduced here):** our `GateAgent` inspects `messages[-1]` only; round-robin order is domain→shell→gate, so the gate always saw *shell's* message and never checked domain's blocks. Reproduced: domain writing `Assets/Hacked.cs` got an empty verdict and the executor ran it. `GuardedExecutor` drops the whole message — same test now executes nothing.
- **Also fixed:** blocks quoted in the task/tool output no longer run (source filter); destructive screen is ~15 regex patterns vs 13 substrings; executor output clipped to ~7.5k; budget counted per HTTP attempt; retry/backoff; checkpoint+resume; `AllAgentsDone` requires both agents; task message pinned in context.
- **Gaps found:** (1) `git push` guard bypassable — `subprocess.run(["git","push"])`, `git -c x push`, `git.exe push` all return None; needs a pre-push hook, not regex. (2) `WaitableAgent` still wakes a *finished* agent on every peer message (~1 wasted request/round). (3) `.env.example` missing `MAX_REQUESTS`/`SEGMENT_MESSAGES`/`LLM_TIMEOUT`; UI `max_turns` now ignored (`max_rounds` param unused).
- **Next Steps:** apply on a branch, add the 3 fixes, re-run both suites.
- **Blockers:** None. Deps live in `C:\Users\Admin\.workbuddy-ai\binaries\python\envs\default`; set `DEEPSEEK_API_KEY` to any dummy value for offline tests.

## Handoff prompt written

- **Status:** DONE. `AI_HANDOFF_PROMPT.md` (250 lines) added at repo root.
- **What it covers:** role briefing, what the project is, hard facts table, architecture diagram, the 9 load-bearing features, 7 invariants, AutoGen 0.7.5 landmines, run/verify commands, current defects (gate bug reproduced, patch verdict + 3 gaps), rules of engagement, first-ten-minutes checklist.
- **Next Steps:** hand it to the next agent verbatim; it deliberately duplicates `PORT_LOG.md` so a fresh session needs no archaeology.
## Rewrite on Microsoft Agent Framework (branch maf-rewrite)

- **Status:** DONE — new package `orchestrator/` replaces `autogen_gui/`. 50/50 offline + WS e2e PASSED, 0 API calls. Not yet merged to `main`, not yet run against the real model.
- **What was done:** MAF 1.22 workflow `seed → domain → coordinator → shell → coordinator` (cycle). Deleted WaitableAgent/[WAIT], IncrementalExecutor, GateAgent and the Supervisor — the typed graph makes all four unnecessary, and scope moved into the tools so a cross-scope write can never be queued (this is the old gate bug, now unrepresentable). Added scoped tools (read/list/write/run_shell/log_progress), destructive screen with token-level `git push` detection, budget+retry as chat middleware, `FileCheckpointStorage`, coordinator stop conditions (complete/budget/stalled/max_rounds/fatal), FastAPI+WS UI, new README/install.ps1/requirements/.env.example.
- **API facts verified by introspection:** `BaseAgent.run` must be a plain def; `AgentExecutor` emits `AgentExecutorResponse` (strict edge typing); `ctx.send_message(target_id=...)`, returning without sending converges; tools' `.invoke()` returns a list of `Content`; middleware is `async (ChatContext, call_next)`.
- **Next Steps:** wire `MCPStdioTool` for rea+unity, port the headless `--resume` CLI, run one short live test, then merge to `main`.
- **Blockers:** None. MAF install downgraded fastapi 0.148→0.141 in the shared venv (harmless).

## MCP wired + first live run (maf-rewrite)

- **Status:** DONE. Offline suite 56/56, WS e2e green, first real-model run PASSED.
- **MCP:** `core.make_mcp_tools()` builds `MCPStdioTool`s from `MCP_SERVERS` (default `rea,unity`); `run_workflow` connects each with `MCP_TIMEOUT` (60s), drops failures, and warns when a server connects with **0 tools**. Measured: `rea` = 170 tools in 8.0s; `unity` = 0 tools in 1.2s (no Editor running — expected).
- **Live smoke test:** 2 rounds, **3 requests**, 8.1s, stopped on `max_rounds`. Confirms one request per agent turn and that the budget middleware counts exactly.
- **Next Steps:** real overnight run needs the Unity Editor up (else the shell agent has no MCP tools); then merge to `main`.
- **Blockers:** None.

## Unity Editor up, MCP verified end to end

- **Status:** DONE. Editor headless on `D:/Flyff source code` (Unity 6000.3.25f1, `-batchmode -nographics`), pipeline on port 7800, MCP exposes **160 tools**.
- **Note:** `unity status` prints only the header even when the pipeline is up — port 7800 answers `401` and the CLI cannot read it. Do not use `unity status` as the readiness check; probe port 7800 or count MCP tools.
- **Live run with both servers:** rea 170 + unity 160 attached, 1 request, 17.8 s (vs 8.1 s with MCP off). Coordinator stopped on `max_rounds`.
- **Cost measurement:** tool schemas are ~19.0k chars (rea) + ~17.9k chars (unity) ≈ **9.2k tokens per request** on every turn. At 4800 requests that is ~44M tokens of pure schema overhead.
- **Next Steps:** consider per-agent MCP routing (domain → rea only, shell → unity only) to halve the payload; then merge to `main`.
- **Blockers:** None.

## Per-agent MCP routing (opt-in)

- **Status:** DONE. `MCP_ROUTING=shared|split`; default `shared` keeps the old behaviour. `split` gives domain→rea, shell→unity.
- **Measured (1 round, 1 request each):** shared 17.8s vs split 15.4s wall — but ~9s of that is MCP startup, so the per-request delta is roughly 8.8s → 6.4s. Schema payload drops from ~9.2k to ~4.8k tokens per request.
- **Tests:** offline suite now 61 checks (5 new routing checks). WS e2e still green.

## Overnight limits: rounds were the wrong axis

- **Status:** DONE. Offline suite 68/68, WS e2e green.
- **Problem:** `MAX_ROUNDS=50` cannot carry a night. One round is a single domain turn plus a
  single shell turn, so 50 rounds is roughly 20 minutes of work — the run would have stopped on
  its own well before morning.
- **Fix:** three independent limits, checked in this order: `complete`, `budget`, `fatal`,
  **`deadline`** (new), `max_rounds`, `stalled`.
  - `MAX_REQUESTS` 4800 — every HTTP attempt, retries included. ~2 per round ⇒ ~2400 rounds.
  - `MAX_HOURS` 8 (new) — wall clock from coordinator construction; `0` disables.
  - `MAX_ROUNDS` 0 (new default) — `0` means unlimited; kept only as a runaway-loop guard.
- **Second bug found while fixing the first:** `WorkflowBuilder(max_iterations=max(200, (max_rounds
  or 1000) * 4 + 8))` capped the *graph* at 4008 steps. At ~25 s per round an 8-hour run needs
  ~4800–5600 steps, so MAF would have raised `WorkflowConvergenceException` around hour 7 — the
  framework, not the policy, would have ended the night. Now derived from the budget
  (`2 × limit + 16` ⇒ 9616 steps) and clamped by `4 × max_rounds + 8` only when set.
- **Wiring:** `Coordinator(max_hours=..., clock=...)` (injectable clock makes the deadline
  testable without waiting); `build_workflow(..., max_hours)`; `run_workflow(..., max_hours)`;
  `/api/config` exposes `max_hours`; the UI shows `N requests · Nh · no round cap`.
- **Docs:** README gained a "Running overnight" table; `.env.example`, `AI_HANDOFF_PROMPT.md`
  and `COLLABORATOR_PROMPT.md` frozen contract updated (the old signature list was stale).
- **Tests added (7):** `max_rounds=0` is unlimited (60 rounds, 120 routes, no stop), keeps routing,
  runs before the deadline, stops on `deadline`, sends nothing past it, `max_rounds` bounds the
  iteration cap, and the iteration cap scales with the budget.
