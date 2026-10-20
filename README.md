# FlyFF Multi-Agent Orchestrator

An autonomous, overnight coding orchestrator built on the **Microsoft Agent Framework (MAF 1.x)**.
Two specialised agents work in parallel on different directories of a large C++ → C#/Unity port
(FlyFF → Unity 6 / FishNet), while a deterministic coordinator routes their turns and enforces
scope at the tool boundary.

This repository is the **orchestrator**, not the game. The agents edit a separate Unity project
(default `D:\Flyff source code`, override with `FLYFF_PROJECT_ROOT`).

> Rewritten from an AutoGen 0.7.5 implementation. That version is on `main`; this one lives on
> `maf-rewrite` until it is promoted.

---

## Why MAF

The AutoGen version worked but carried four workarounds, three of which existed only because a
group chat broadcasts every message to every participant:

| AutoGen workaround | Why it existed | In MAF |
|---|---|---|
| `[WAIT]` token + `WaitableAgent` | Round-robin woke agents that had nothing to do | Executors fire only when typed inputs arrive — idle is free by construction |
| `IncrementalExecutor` | `CodeExecutorAgent` re-extracted the whole thread every turn | Edges carry only new messages |
| `GateAgent` | Could only annotate the thread; it could not un-queue a blocked code block | Scope is enforced inside the tools, so a cross-scope write never reaches execution |
| Hand-rolled `Supervisor` checkpoints | No built-in durability | `FileCheckpointStorage` + `checkpoint_id` |

It also fixed a real defect: the old gate inspected `messages[-1]`, and because the turn order was
`domain → shell → gate`, it never saw the domain agent's output. A domain write to `Assets/` passed
the gate and was executed. That failure mode does not exist here — `write_file` refuses writes
outside the calling agent's scope.

---

## Architecture

```
   seed(task)
       │
       ▼
  ┌─────────┐   AgentExecutorResponse   ┌──────────────┐
  │ domain  │ ─────────────────────────▶│ coordinator  │
  │  agent  │                           │  (no model)  │
  └─────────┘                           └──────┬───────┘
       ▲                                       │
       │                                       ▼
  ┌─────────┐                           ┌──────────────┐
  │  shell  │◀──────────────────────────│  routes to   │
  │  agent  │                           │  the other   │
  └─────────┘                           │  agent, or   │
                                        │  stops, which│
                                        │  converges   │
                                        └──────────────┘
```

The coordinator is the only node that decides what happens next. It forwards the response to the
other agent, or emits nothing, which converges the workflow. Stop reasons: `complete`, `budget`,
`stalled`, `max_rounds`, `fatal`.

**Turn data.** Each `AgentExecutorResponse` carries the full conversation with `author_name`
preserved, so any node can attribute a message to its author — the thing AutoGen could not do.

---

## Features

| Feature | How |
|---|---|
| **Scoped writes** | `write_file` refuses any path outside the agent's scope; `run_shell` screens cross-scope redirects and `cp`/`mv` destinations |
| **Destructive-command screen** | Regex + token screen for `git push` (including `subprocess.run(["git","push"])`), `reset --hard`, `clean -fdx`, recursive deletes outside `obj/bin/Temp`, disk and power commands |
| **MCP tools** | `rea` (170 tools) and `unity` (160) attached via `MCPStdioTool`; `MCP_ROUTING=shared` gives both to both agents, `split` gives domain→rea and shell→unity (~9.2k → ~4.8k tokens of schema per request). A server that fails to start, or connects with 0 tools, is dropped with a note |
| **Bounded context** | `context_mode="custom"` + `context_filter`: the task message plus the last `HISTORY_BUFFER` messages, so cost does not grow with turn count |
| **Request budget** | Chat middleware counts every attempt (retries included) and refuses past `MAX_REQUESTS` |
| **Retry with backoff** | Same middleware: transient errors retried 6×, auth errors fail fast after 3, every attempt counted |
| **Checkpoint / resume** | `FileCheckpointStorage` per run; `resume` restores the last checkpoint |
| **Progress notes** | `log_progress` appends to `PORT_LOG_<agent>.md` in the target repo so a restart can pick up |
| **Chat UI** | Bubbles with timestamps: domain left/blue, shell right/green, coordinator centred grey; Copy Chat; debug toggle |

---

## Install

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The script finds (or installs) Python 3.12+ and Node.js, creates `venv/`, installs
`requirements.txt`, copies `.env.example` to `.env` without overwriting an existing one, and starts
the server.

### Manual

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then fill in DEEPSEEK_API_KEY
.\venv\Scripts\python.exe -m orchestrator.server
```

Open <http://127.0.0.1:8765>.

---

## Configure

```ini
DEEPSEEK_API_KEY=sk-...                  # required; read from the environment only
DEEPSEEK_BASE_URL=https://www.aibh.cc/v1
DEEPSEEK_PRO_MODEL=deepseek-v4-pro       # domain agent (reasoning)
DEEPSEEK_FLASH_MODEL=deepseek-v4.1-flash # shell agent (fast)
FLYFF_PROJECT_ROOT=D:\Flyff source code  # the repo the agents may touch

MAX_REQUESTS=4800    # hard cap (provider limit is 5000)
MCP_SERVERS=rea,unity  # or `rea`, `unity`, or empty to disable
MCP_ROUTING=shared     # or `split`: domain->rea, shell->unity (halves the payload)
MAX_ROUNDS=50
HISTORY_BUFFER=16
SHELL_TIMEOUT=600
```

Any OpenAI-compatible endpoint works: `OpenAIChatCompletionClient(base_url=...)`.

---

## Use

Type a kickoff instruction (or leave it empty for the built-in one) and press **Run**.

- **Show debug** — reveals coordinator notes and per-executor status events.
- **Copy Chat** — exports the transcript as `[hh:mm:ss] speaker: text`.
- **Stop** — cancels the run; the checkpoint stays on disk.

Each run writes to `orchestrator/runs/<timestamp>/`: `budget.json`, `coordinator.json`,
`checkpoints/`, `summary.json`. These are git-ignored.

---

## Verify

```powershell
$env:DEEPSEEK_API_KEY="sk-offline-verify"   # any dummy value; these make no API calls
python orchestrator\verify_offline.py       # 50 checks
python orchestrator\test_ws.py              # end-to-end WebSocket run with fake agents
```

`verify_offline.py` covers: config, scope enforcement (including the cross-scope case that the old
gate missed), the destructive-command screen (10 blocked, 5 allowed), the scoped tools, bounded
context, every coordinator stop condition, a full workflow run with fake agents, checkpoint
creation, and budget accounting with retries.

`test_ws.py` drives the real FastAPI app over a WebSocket with fake agents: start → bubbles →
`done`, zero requests.

Live tests cost real requests. Do not run them to check a code change.

---

## Layout

```
.
├── orchestrator/
│   ├── core.py            config, scoped tools, destructive screen, budget middleware
│   ├── workflow.py        prompts, agents, coordinator, graph, run loop
│   ├── server.py          FastAPI + WebSocket
│   ├── index.html         chat UI (no build step)
│   ├── verify_offline.py  offline suite
│   └── test_ws.py         WebSocket end-to-end test
├── install.ps1
├── requirements.txt
├── .env.example
└── verify_models.py       model/tool-calling probe
```

---

## Security

- API keys live only in `.env` (git-ignored). `core.resolve_api_key()` raises if it is missing.
- **This is not a sandbox.** `run_shell` executes commands with your privileges. The screen blocks
  known-destructive patterns, including token-level `git push` detection, but an agent that
  constructs a command cleverly can still get through. Run it on a machine and repository you are
  willing to let an autonomous agent modify, and keep version control clean.
- Scope enforcement covers file writes and shell redirects. MCP tools (Unity, rea) are **not**
  scope-checked — a Unity MCP call can touch `Assets/` regardless of which agent made it.
- Agents commit locally; `git push` is refused. Push by hand after reviewing.

---

## License

Unlicensed / private. Add a license before public distribution.
