# AutoGen FlyFF Multi-Agent Orchestrator

A specialized, fully autonomous multi-agent system built on **AutoGen 0.7** that drives
long-running software engineering work on a large codebase. It was built for the
FlyFF → Unity 6 / FishNet port, but the architecture is project-agnostic.

Two specialised coding agents work in parallel, a deterministic **GateAgent** enforces
directory ownership at the code-execution boundary, and an **Executor** runs the shell
commands and test suites they produce. Optional MCP servers expose reverse-engineering
and Unity Editor tooling to both agents.

---

## Purpose

Autonomous agents fail on large repositories for predictable reasons: they burn the
context window re-reading logs, they wander into files they do not own, and they
re-explore the same code every turn. This system addresses each of those directly
(see *Features*), so a run can proceed unattended for hours within a fixed request
budget.

---

## Features

| Feature | What it does |
|---|---|
| **Bounded context** | Each agent replays at most `HISTORY_BUFFER` messages per turn, so cost does not grow with turn count. |
| **GateAgent scope enforcement** | Blocks cross-scope writes (`domain` → `Assets/`, `shell` → `Domain/`) *before* the executor sees them. The executor cannot attribute code blocks to authors, so a dedicated stage is required. |
| **`[WAIT]` optimisation** | An agent with nothing to do replies `[WAIT]` and its next turn is answered locally — **zero API requests**. Reactivates automatically when the other agent produces work. |
| **Incremental execution** | Only code blocks newer than the executor's last turn are run, preventing replay of historical commands (`git commit`, builds) every cycle. |
| **MCP workbenches** | Both agents get both MCP servers (Unity Editor control, REA binary analysis) merged with their local file tools. |
| **Copy Chat** | One click exports the whole conversation (timestamps + speakers) to the clipboard. |
| **Live LLM trace** | Optional debug panel showing prompts, replies, tool calls and token usage per call. |

---

## How to Install

### One-click (recommended)

```powershell
# Right-click install.ps1 → "Run with PowerShell", or:
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The script will:

1. Locate Python (installing 3.12 via `winget` if missing).
2. Locate `npx` (installing Node.js LTS if missing).
3. Create `venv/` and `pip install -r requirements.txt`.
4. Create `.env` from `.env.example` (never overwriting an existing key).
5. Prompt you to fill in your API keys.
6. Start the server on <http://127.0.0.1:8765>.

### Manual

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then edit .env
.\venv\Scripts\python.exe -m uvicorn autogen_gui.server:app --host 127.0.0.1 --port 8765
```

---

## How to Use

### 1. Configure API keys

Edit `.env`:

```ini
DEEPSEEK_API_KEY=sk-...            # required
DEEPSEEK_BASE_URL=https://www.aibh.cc/v1
DEEPSEEK_PRO_MODEL=deepseek-v4-pro      # Agent A (reasoning)
DEEPSEEK_FLASH_MODEL=deepseek-v4.1-flash # Agent B (fast)
FLYFF_PROJECT_ROOT=D:\Flyff source code  # repo the agents may touch
```

Any OpenAI-compatible endpoint works. `.env` is git-ignored and never read from source
code — the key exists only in the environment.

### 2. Start the server

The installer starts it for you. Otherwise:

```powershell
.\venv\Scripts\python.exe -m uvicorn autogen_gui.server:app --host 127.0.0.1 --port 8765
```

### 3. Open the UI

Browse to <http://127.0.0.1:8765>. The sidebar shows project files (name, size, line
count) and any open decisions. It never sends file bodies to the model.

### 4. Launch a task

Type the kickoff instruction and press **Run**. The prefilled message assigns backlog
items to each agent. Watch messages stream as chat bubbles: **Agent A** left/blue,
**Agent B** right/green, **Executor** and **Gate** centred grey.

- **Show debug** — reveals per-call LLM traces (prompts, replies, tokens).
- **Copy Chat** — exports the transcript with timestamps.
- **Stop** — cancels the run immediately.

### 5. Optional: Unity MCP

Unity MCP is a *proxy* to a running Editor and requires the pipeline package:

```powershell
unity pipeline install --project-path "D:\Flyff source code"
# restart the Editor afterwards — the package only loads at boot
unity status            # expect: <port>  ready  <project>
```

Without a running Editor the Unity server connects but exposes **0 tools** (silently).

---

## Architecture

```
                        ┌─────────────────────────────┐
   user task ──────────▶│  RoundRobinGroupChat        │
                        └──────────────┬──────────────┘
                                       │  turns cycle A → B → Gate → Executor
              ┌────────────────────────┼────────────────────────┐
              ▼                        ▼                        ▼
     ┌────────────────┐      ┌────────────────┐        ┌──────────────────┐
     │ Agent A        │      │ Agent B        │        │ GateAgent        │
     │ "domain"       │      │ "shell"        │        │ (no model)       │
     │ pro model      │      │ flash model    │        │ scope check      │
     │ owns Domain/   │      │ owns Assets/   │        │ blocks cross-    │
     │      Tools/    │      │  Server/ Client│        │ scope writes     │
     └───────┬────────┘      └───────┬────────┘        └────────┬─────────┘
             │                       │                          │
             │  workbench:           │  workbench:              │ pass
             │  file tools +         │  file tools +            ▼
             │  REA MCP + Unity MCP  │  REA MCP + Unity MCP  ┌──────────────────┐
             └───────────┬───────────┘                       │ Executor         │
                         │  ```python / ```sh blocks         │ (no model)       │
                         └──────────────────────────────────▶│ runs commands,   │
                                                              │ returns raw      │
                                                              │ output to agents │
                                                              └──────────────────┘
```

**Flow:** an agent writes code (a file write via its scoped tool, or a ` ```sh ` block
for anything else). The Gate inspects the pending message; a cross-scope write is
refused with `BLOCKED` and never executes. Otherwise the Executor runs only the *new*
blocks and feeds raw stdout/stderr back into the thread, where the agents fix errors
on their next turn.

**Termination:** any message containing `OVERNIGHT_COMPLETE` or `BUDGET_EXHAUSTED`, or
the `max_rounds` cap.

---

## Project layout

```
.
├── install.ps1              # one-click installer/launcher
├── requirements.txt
├── .env.example             # copy to .env and fill in
├── .gitignore
├── autogen_gui/
│   ├── server.py            # FastAPI + WebSocket backend, agents, tools
│   ├── index.html           # chat UI
│   ├── verify_offline.py    # offline test suite (no API calls)
│   ├── test_e2e.py          # live WebSocket test (spends requests)
│   └── test_mcp.py          # MCP connectivity probe
└── README.md
```

---

## Verification

```powershell
.\venv\Scripts\python.exe autogen_gui\verify_offline.py
```

Runs 33 checks with **no API calls**: MCP parameter sanity, team composition, scope
enforcement, gate behaviour, `[WAIT]` economics, incremental execution, denylist, and
that the context endpoint returns summaries only.

---

## Security notes

- API keys live only in `.env` (git-ignored). `server.py` reads them from the
  environment and refuses to start without one.
- The Executor runs model-generated code with your full user privileges. A denylist
  blocks drive-wrecking patterns only — it is **not** a sandbox. Run this on a machine
  and repository you are willing to let an autonomous agent modify, and keep version
  control clean so bad changes are recoverable.
- Scope enforcement protects `Domain/` vs `Assets/` ownership between agents; it does
  not protect against deliberate bypass via an arbitrary shell command we did not
  pattern-match.

---

## License

Unlicensed / private. Add a license before public distribution.
