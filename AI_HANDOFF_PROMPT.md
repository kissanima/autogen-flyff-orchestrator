# AI Handoff Prompt — AutoGen FlyFF Multi-Agent Orchestrator

> Paste this file (or its path) at the start of a session with any capable coding agent.
> It is written as a direct briefing. Read it fully before touching code.

---

## 1. Your role

You are taking over a working, published project. It is not a greenfield build and it is not a
toy demo. Your job is to continue it without breaking the invariants in section 5, and without
burning the owner's API budget discovering things this document already states.

Do not re-derive the architecture from scratch. Do not rewrite the orchestration layer because
you prefer a different pattern. Extend what is there.

---

## 2. What the project is

An **autonomous, overnight coding orchestrator** built on **AutoGen 0.7.5**. Two LLM agents work
in parallel on different directories of a large C++ → C#/Unity port (FlyFF → Unity 6 / FishNet),
while a deterministic stage enforces directory ownership and a model-free executor runs the
shell commands they produce.

The system exists because autonomous agents fail on large repositories in three predictable ways:

1. They burn the context window re-reading logs and re-exploring code every turn.
2. They wander into files they do not own.
3. They stop and wait for a human when something breaks.

Each feature in section 4 exists to defeat one of those three failures. If you propose a change,
say which of the three it serves — or explain why it serves something else.

**The target repository is NOT this repository.** This repo is the orchestrator. The agents edit a
separate Unity project (default `D:\Flyff source code`, override with `FLYFF_PROJECT_ROOT`).

---

## 3. Hard facts

| Item | Value |
|---|---|
| Stack | Python 3.12+, AutoGen 0.7.5 (`autogen-agentchat`, `autogen-ext[mcp,openai]`), FastAPI, uvicorn |
| Entry (UI) | `python -m uvicorn autogen_gui.server:app --host 127.0.0.1 --port 8765` |
| UI | Single-page `autogen_gui/index.html`, WebSocket chat bubbles, no build step |
| Models | `domain` = `deepseek-v4-pro` (reasoning), `shell` = `deepseek-v4.1-flash` (fast) |
| Provider | Any OpenAI-compatible endpoint; default `https://www.aibh.cc/v1` |
| Secrets | `.env` only. `server.py` refuses to start without `DEEPSEEK_API_KEY`. Never hardcode. |
| MCP | `rea` (`npx -y rea-agents@6.4.0 mcp`, 170 tools) + `unity` (`unity mcp`, 160 tools) |
| Repo | `https://github.com/kissanima/autogen-flyff-orchestrator` (private), branch `main` |
| Tracked files | 15. `PORT_LOG.md` is the running handoff log — read it second, after this file. |

---

## 4. Architecture as it exists on `main` today

```
user task ──▶ RoundRobinGroupChat
                 │  turns cycle: domain → shell → gate → executor
                 ▼
      ┌──────────────┐   ┌──────────────┐   ┌───────────┐   ┌────────────┐
      │ Agent A      │   │ Agent B      │   │ GateAgent │   │ Executor   │
      │ "domain"     │   │ "shell"      │   │ no model  │   │ no model   │
      │ pro model    │   │ flash model  │   │ scope     │   │ runs code  │
      │ Domain/      │   │ Assets/      │   │ check     │   │ blocks     │
      │ Tools/       │   │ Server/      │   │           │   │            │
      │              │   │ Client/      │   │           │   │            │
      └──────┬───────┘   └──────┬───────┘   └─────┬─────┘   └─────┬──────┘
             │  workbench:      │  workbench:      │               │
             │  file tools +    │  file tools +    │               │
             │  rea MCP +       │  rea MCP +       │               │
             │  unity MCP       │  unity MCP       │               │
             └──────────────────┴──────────────────┴───────────────┘
```

### Features that matter (each one is load-bearing)

| Feature | Mechanism | Why it exists |
|---|---|---|
| **Bounded context** | `BufferedChatCompletionContext(buffer_size=12)` on both agents | Per-turn cost must not grow with turn count |
| **`[WAIT]` economics** | `WaitableAgent` returns `[WAIT]` **without calling the model** while the peer has produced nothing | An idle agent must cost zero requests |
| **Scope enforcement** | `WRITE_SCOPES = {"domain": ("Domain","Tools"), "shell": ("Assets","Server","Client")}` checked in `write_file` and at the executor boundary | Agents must not edit each other's directories |
| **Incremental execution** | `IncrementalExecutor` only runs code blocks newer than its own last turn | Otherwise every cycle re-runs `git commit`, `dotnet`, MCP calls |
| **MCP workbenches** | `CompositeWorkbench` merges local `FunctionTool`s + two `McpWorkbench`s per agent | AutoGen forbids `tools=` together with `workbench=` |
| **Destructive screen** | `SHELL_DENYLIST` substring check in `SafeLocalExecutor` | Blocks drive-wrecking patterns only. **Not a sandbox.** |
| **Chat UI** | `SIDES` map: domain left/blue, shell right/green, executor+gate centered grey | Readable transcript of a 4-way conversation |
| **Copy Chat** | Timestamped transcript export to clipboard | Post-mortem without the server logs |
| **Live trace** | Optional per-call prompt/reply/token panel behind "Show debug" | Diagnose cost and looping |

### Termination

`TextMentionTermination("OVERNIGHT_COMPLETE") | TextMentionTermination("BUDGET_EXHAUSTED") |
MaxMessageTermination(max(2, min(max_rounds, 150)))`, default `max_rounds=80`.

---

## 5. Invariants — do not break these

1. **Secrets live in `.env` and nowhere else.** `resolve_api_key()` must keep raising if the key is
   absent. No fallback to a config file, no default string.
2. **`HISTORY_BUFFER` stays bounded.** Any change that lets full history replay per turn is a
   regression, not an improvement.
3. **`[WAIT]` must remain zero-cost.** If a code path calls the model to decide whether to wait,
   the economics are gone.
4. **`WRITE_SCOPES` is the contract.** Domain agents write C# with zero UnityEngine references;
   shell agents write Unity/FishNet code. Cross-scope writes must be refused, not warned about.
5. **The executor runs only what the agents just wrote.** Never re-run historical code blocks.
6. **No `git add -A` / `git add .`** — in this repo or in the target repo. Stage explicit paths.
   Build artifacts (`bin/`, `obj/`, `Library/`) are never committed.
7. **`PORT_LOG.md` gets a status entry at the end of every work session** (Status / What was done /
   Next Steps / Blockers, under 20 lines). Your context will reset; this is the only memory.

---

## 6. AutoGen 0.7.5 landmines (learned the hard way — do not rediscover)

- `StdioServerParams` defaults `read_timeout_seconds` to **5s**. `npx` cold start exceeds it and
  silently kills the session. We use **60**.
- `ToolSchema` is a **TypedDict**, not a class. `ToolSchema(...)` returns a plain dict. Access
  `schema["name"]`, never `schema.name`.
- `Workbench` and `ChatAgent` are ABCs. A custom `ChatAgent` must implement `on_messages_stream`,
  `on_messages`, `on_reset`, `on_pause`, `on_resume`, `save_state`, `load_state`, `close`,
  `name`, `description`, `produced_message_types`.
- A `Workbench` must implement `list_tools`, `call_tool`, `start`, `stop`, `reset`.
- There is **no manager injection** in 0.7.5. Do not try to subclass the group chat manager to
  intercept turns; wrap the participants instead.
- `CodeExecutorAgent` reads the **whole thread** and re-extracts every code block, including ones
  quoted in the task message. Override `extract_code_blocks_from_messages`.
- A team that raises is permanently "stopped" — calling `run_stream` again returns instantly.
  Recovery means building a **new team**, not retrying the old one.
- `CodeExecutorAgent`'s denylist override must be **async** or it returns a coroutine instead of a
  `CodeResult`.
- Unity MCP is a **proxy**. With no running Editor (and no `com.unity.pipeline` package) it
  connects fine and exposes **0 tools**, silently. Fix: `unity pipeline install --project-path
  "<repo>"`, then restart the editor; `unity status` should say `ready`.

---

## 7. How to run

```powershell
# install (idempotent; never overwrites an existing .env)
powershell -ExecutionPolicy Bypass -File .\install.ps1

# or manual
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then fill in DEEPSEEK_API_KEY
.\venv\Scripts\python.exe -m uvicorn autogen_gui.server:app --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>, type the kickoff instruction, press **Run**.

**On this machine** there is no `venv/` in the repo. AutoGen is installed at
`C:\Users\Admin\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe`. Use that
interpreter for tests.

---

## 8. How to verify (never declare done without this)

```powershell
$env:DEEPSEEK_API_KEY="sk-offline-verify"    # any dummy value; these suites make no API calls
python autogen_gui\verify_offline.py         # 33 checks: team shape, scopes, [WAIT], context
python autogen_gui\test_mcp.py               # MCP connectivity (needs the servers running)
```

`test_e2e.py` and `autogen_live_test.py` **spend real requests**. Do not run them without asking.

A green suite is the minimum bar. It is not proof of correctness — see section 9.

---

## 9. Current state and known defects (as of 2026-10-20)

### Confirmed bug: the GateAgent checks the wrong message

`GateAgent.on_messages_stream` inspects `messages[-1]` only. Round-robin order is
`domain → shell → gate → executor`, so when the gate runs, the last message is **shell's**. The
domain agent's code blocks are never scope-checked.

Reproduced on `main`:

```
participant order: ['domain', 'shell', 'gate', 'executor']
gate verdict on domain cross-scope write: ''
executor would run: ['echo pwn > "Assets/Hacked.cs"']
```

The gate passed it and the executor queued the write. The existing test missed this because it
called the gate with a **single** message, not the real two-message tail.

Second half of the same bug: even when the gate returns `BLOCKED`, the executor still runs the
offending blocks — a gate can annotate the thread but cannot un-queue work. Enforcement has to
live **inside the executor**.

### Reviewed but NOT applied: Claude's `unattended-mode` patch

Location: `C:\Users\Admin\WorkBuddy AI\Claude patch\` (`unattended-mode.patch` + `files.zip`).
Extracted copy for reading: `Claude patch/review/autogen-flyff-orchestrator/`.

It adds `autonomy.py` (638 lines: `BudgetTracker`, `ResilientClient`, `PinnedBufferedContext`,
`AllAgentsDone`, `command_violation`, `Supervisor`), `overnight.py` (143 lines: headless runner,
`--resume`, git preflight, keep-awake), a 443-line `verify_unattended.py`, and replaces
`GateAgent` with `GuardedExecutor`. Both suites pass offline (39 + 6 groups).

**Verdict: merge it.** Three gaps to close first:

1. **`git push` guard is bypassable.** `command_violation` misses `git -c x push`, `git.exe push`,
   `subprocess.run(["git","push"])`, `os.system("git " + "push")`. Fix with a `pre-push` hook in
   the target repo (exit non-zero while a run marker exists), not with more regex.
2. **A finished agent keeps waking.** `WaitableAgent` reactivates on any non-`[WAIT]` peer message
   and never checks `AllAgentsDone.done`. An agent that already reported `OVERNIGHT_COMPLETE`
   still costs ~1 request per round for the rest of the night.
3. **Doc drift.** `.env.example` lacks `MAX_REQUESTS` / `SEGMENT_MESSAGES` / `LLM_TIMEOUT`; the UI
   still sends `max_turns`, but `max_rounds` is now an unused parameter in `run_agent_task`.

### Known non-defects (do not "fix" these)

- `TASK_COMPLETE: <System>` (per backlog item) and `OVERNIGHT_COMPLETE` (whole backlog) are
  different tokens on purpose.
- The executor has **no sandbox**. It runs LLM-written code with the owner's privileges. This is a
  deliberate, documented trade-off on a trusted machine.
- MCP tools are **not** scope-checked. Scope applies to file writes and shell blocks.

---

## 10. Rules of engagement

- **Verify, do not guess.** Read the file, run the command. No invented APIs.
- **Do not run the agents to test a code change.** Offline suites exist for that. A live run costs
  real requests and can take hours.
- **Scope isolation.** Touch only what the task names. Do not reformat unrelated code.
- **Commit atomically**, explicit paths only, descriptive messages. Never commit `.env`, `venv/`,
  `__pycache__/`, `runs/`, or build output.
- **If a change is architectural** (new participant, new termination strategy, different model
  topology), state the plan and ask before implementing.
- **Record the outcome** in `PORT_LOG.md` before you finish.

---

## 11. First ten minutes

1. Read `PORT_LOG.md` — it has the full history and the current blockers.
2. Read `autogen_gui/server.py` top to bottom. It is ~900 lines and everything lives there.
3. Run `verify_offline.py` to confirm your environment is sane.
4. Read section 9 and decide whether you are applying the patch or fixing `main` in place.
5. Do not start an agent run until you can explain, in one sentence each, what `[WAIT]`,
   `WRITE_SCOPES`, and `IncrementalExecutor` do.
