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