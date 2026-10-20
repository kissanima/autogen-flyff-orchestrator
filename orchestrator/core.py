"""Configuration, scoped tools, and the request-budget middleware.

Everything here is framework-agnostic except `budget_middleware`, which uses the
Microsoft Agent Framework chat-middleware protocol.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

WORKSPACE = Path(__file__).resolve().parent

# .env next to this package, or one level up (repo root).
for _candidate in (WORKSPACE / ".env", WORKSPACE.parent / ".env"):
    if _candidate.exists():
        load_dotenv(_candidate)
        break
else:
    load_dotenv()

# Never let git block an unattended run waiting for an editor or credentials.
os.environ.setdefault("GIT_EDITOR", "true")
os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")

PROJECT_ROOT = Path(os.getenv("FLYFF_PROJECT_ROOT", r"D:\Flyff source code"))
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://www.aibh.cc/v1")
MODEL_PRO = os.getenv("DEEPSEEK_PRO_MODEL", "deepseek-v4-pro")
MODEL_FLASH = os.getenv("DEEPSEEK_FLASH_MODEL", "deepseek-v4.1-flash")
MODELS = {"domain": MODEL_PRO, "shell": MODEL_FLASH}

HISTORY_BUFFER = int(os.getenv("HISTORY_BUFFER", "16"))
MAX_REQUESTS = int(os.getenv("MAX_REQUESTS", "4800"))
MAX_ROUNDS = int(os.getenv("MAX_ROUNDS", "50"))
SHELL_TIMEOUT = int(os.getenv("SHELL_TIMEOUT", "600"))
MAX_LIST_ENTRIES = 200
MAX_READ_LINES = 400
OUTPUT_HEAD, OUTPUT_TAIL = 1500, 6000

RUNS_DIR = WORKSPACE / "runs"

# MCP servers exposed to both agents. `rea` needs npx (downloads on first use);
# `unity` is a proxy that needs a running Editor with com.unity.pipeline installed,
# otherwise it connects and exposes zero tools.
MCP_TIMEOUT = int(os.getenv("MCP_TIMEOUT", "60"))
MCP_ENABLED = os.getenv("MCP_SERVERS", "rea,unity")
MCP_SERVERS: dict[str, dict[str, Any]] = {
    "rea": {"command": "npx", "args": ["-y", "rea-agents@6.4.0", "mcp"]},
    "unity": {"command": "unity", "args": ["mcp", "--project-path", str(PROJECT_ROOT)]},
}


def make_mcp_tools() -> list[Any]:
    """Construct (but do not connect) the configured MCP servers.

    Set MCP_SERVERS=rea (or unity, or empty) to choose. Connection is the caller's
    job because MCPStdioTool is an async context manager.
    """
    from agent_framework import MCPStdioTool

    wanted = [n.strip() for n in MCP_ENABLED.split(",") if n.strip()]
    tools = []
    for name in wanted:
        cfg = MCP_SERVERS.get(name)
        if not cfg:
            continue
        tools.append(MCPStdioTool(
            name=name, command=cfg["command"], args=list(cfg["args"]),
            request_timeout=MCP_TIMEOUT, load_prompts=False,
        ))
    return tools


WAIT_TOKEN = "[WAIT]"
DONE_TOKEN = "OVERNIGHT_COMPLETE"

WRITE_SCOPES: dict[str, tuple[str, ...]] = {
    "domain": ("Domain", "Tools"),
    "shell": ("Assets", "Server", "Client"),
}
BLOCKED_DIRS = {"Library", "Temp", "Builds", "Logs", "UserSettings", "obj", "bin",
                "PatchStage", ".git"}


def resolve_api_key() -> str:
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set.\n"
            "Copy .env.example to .env and fill in your key, then restart."
        )
    return key


# --------------------------------------------------------------------------
# Path handling
# --------------------------------------------------------------------------

def _resolve(rel: str) -> Path:
    p = (PROJECT_ROOT / rel).resolve()
    root = PROJECT_ROOT.resolve()
    if p != root and root not in p.parents:
        raise PermissionError(f"path escapes the project root: {rel}")
    return p


def _norm_top(raw: str) -> str:
    norm = raw.replace("\\", "/")
    root = str(PROJECT_ROOT).replace("\\", "/").lower()
    norm = norm.lower().replace(root + "/", "").replace(root, "")
    norm = re.sub(r"^[a-zA-Z]:/", "", norm)
    return norm.split("/")[0]


SCOPED_TOPS = ("domain", "assets", "server", "client", "tools")

_WRITE_TARGET = re.compile(
    r"""(?:>>?|(?:Set-Content|Add-Content|Out-File|tee|cp|copy|mv|move|New-Item|"""
    r"""Remove-Item|rm|del|mkdir))\s+(?:-\w+\s+)*["']?"""
    r"""([A-Za-z]:[\\/][^\s"'<>|]+|(?:Domain|Assets|Server|Client|Tools)[\\/][^\s"'<>|]*)""",
    re.IGNORECASE,
)


_CMD_DEST = re.compile(
    r"""\b(cp|copy|mv|move|robocopy)\b[^\n;|]*?["']?"""
    r"""([A-Za-z]:[\\/][^\s"'<>|]+|[A-Za-z0-9_.\\/-]+[\\/][^\s"'<>|]*)["']?\s*$""",
    re.IGNORECASE | re.MULTILINE,
)


def scope_violation(text: str, agent: str) -> str | None:
    """Return a description if `text` writes outside `agent`'s scope.

    Covers redirects, write cmdlets, and the *destination* of cp/mv/robocopy (which is
    the last argument, not the first).
    """
    allowed = {s.lower() for s in WRITE_SCOPES[agent]}
    targets = [m.group(1) for m in _WRITE_TARGET.finditer(text)]
    targets += [m.group(2) for m in _CMD_DEST.finditer(text)]
    for raw in targets:
        top = _norm_top(raw)
        if top in SCOPED_TOPS and top not in allowed:
            return (f"'{top.capitalize()}/' is outside {agent}'s scope "
                    f"{WRITE_SCOPES[agent]}")
    return None


# --------------------------------------------------------------------------
# Destructive-command screen
# --------------------------------------------------------------------------

_ALWAYS_BLOCK: tuple[tuple[str, str], ...] = (
    (r"\bgit\s+reset\s+--hard", "git reset --hard"),
    (r"\bgit\s+clean\b[^\n]*\s-\w*[fdx]", "git clean"),
    (r"\bgit\s+(checkout|restore)\b[^\n]*\s(\.|\*)\s*$", "git checkout/restore of the whole tree"),
    (r"\bgit\s+branch\s+-D\b", "git branch -D"),
    (r"\bgit\s+filter-(branch|repo)\b", "git history rewrite"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "git stash drop/clear"),
    (r"\bgit\s+reflog\s+(expire|delete)\b|\bgit\s+gc\b[^\n]*--prune", "git reflog/gc prune"),
    (r"\bgit\s+update-ref\s+-d\b", "git update-ref -d"),
    (r"\bformat(\.com)?\s+[A-Za-z]:", "disk format"),
    (r"\bdiskpart\b|\bmkfs\b|\bdd\s+if=", "disk tool"),
    (r":\(\)\s*\{", "fork bomb"),
    (r"\b(shutdown|Restart-Computer|Stop-Computer)\b", "power control"),
    (r"\breg\s+(add|delete)\b|\bnet\s+user\b|\bSet-ExecutionPolicy\b", "system configuration"),
)
_RECURSIVE_DELETE = re.compile(
    r"\brm\b[^\n]*\s-\w*[rR]|\brm\b[^\n]*--recursive|\b(rd|rmdir)\b[^\n]*/s\b|"
    r"\bdel\b[^\n]*/s\b|\bRemove-Item\b[^\n]*-Recurse|shutil\.rmtree|os\.removedirs",
    re.IGNORECASE)
_SAFE_DELETE_NAMES = {"obj", "bin", "temp", "__pycache__", "node_modules", ".pytest_cache"}
_STATEMENT_SPLIT = re.compile(r"&&|\|\||;|\|")


def _delete_targets(statement: str) -> list[str]:
    if "rmtree" in statement or "removedirs" in statement:
        return re.findall(r"""["']([^"']+)["']""", statement)
    toks = re.findall(r"""\"[^\"]*\"|'[^']*'|[^\s]+""", statement)
    out = []
    for tok in toks[1:]:
        if tok.startswith("-") or re.fullmatch(r"/[A-Za-z]", tok) or tok == "|":
            continue
        out.append(tok.strip("\"'"))
    return out


def _is_safe_delete(statement: str) -> bool:
    """A delete is safe if every target is inside the project and names a build folder.

    `rm -rf obj/Debug` is allowed; `rm -rf /`, `rm -rf ~` and anything outside the
    project root are not.
    """
    targets = _delete_targets(statement)
    if not targets:
        return False
    root = str(PROJECT_ROOT).replace("\\", "/").lower().rstrip("/")
    for tgt in targets:
        norm = tgt.replace("\\", "/").lower().rstrip("/")
        if re.match(r"^[a-z]:/|^/", norm) and not norm.startswith(root):
            return False
        segments = [s for s in norm.split("/") if s and s != "."]
        if not segments or not any(s in _SAFE_DELETE_NAMES for s in segments):
            return False
    return True


def command_violation(code: str) -> str | None:
    """Return a reason if `code` matches a destructive pattern, else None.

    The push check is token based rather than whitespace based: an agent writing
    `subprocess.run(["git", "push"])` must be caught just like `git push`.
    """
    flat = code.replace('"', " ").replace("'", " ").replace(",", " ")
    if re.search(r"\bgit(\.exe)?\b", flat, re.IGNORECASE) and \
            re.search(r"\bpush\b", flat, re.IGNORECASE):
        return "git push (agents commit locally; push by hand)"

    for pat, label in _ALWAYS_BLOCK:
        if re.search(pat, code, re.IGNORECASE | re.MULTILINE):
            return label

    for line in code.splitlines():
        for stmt in _STATEMENT_SPLIT.split(line):
            if _RECURSIVE_DELETE.search(stmt) and not _is_safe_delete(stmt):
                return "recursive delete outside build folders (obj/bin/Temp)"
    return None


def clip_output(text: str, head: int = OUTPUT_HEAD, tail: int = OUTPUT_TAIL) -> str:
    if len(text) <= head + tail + 200:
        return text
    return text[:head] + f"\n...[{len(text) - head - tail} chars omitted]...\n" + text[-tail:]


# --------------------------------------------------------------------------
# Scoped tools (the framework's replacement for the old Executor stage)
# --------------------------------------------------------------------------

def make_tools(agent: str) -> list[Any]:
    """Build the tool set for one agent. Every tool is bound to `agent`."""
    from agent_framework import tool

    scope = WRITE_SCOPES[agent]

    @tool(name="read_file",
          description="Read a UTF-8 text file from the project. Returns numbered lines.")
    def read_file(path: str, max_lines: int = MAX_READ_LINES) -> str:
        """path: project-relative path. max_lines: cap on lines returned."""
        try:
            p = _resolve(path)
        except PermissionError as exc:
            return f"ERROR: {exc}"
        if not p.exists():
            return f"ERROR: no such file: {path}"
        if p.is_dir():
            return f"ERROR: {path} is a directory; use list_dir"
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return f"ERROR: {exc}"
        shown = lines[:max_lines]
        body = "\n".join(f"{i + 1:5d}  {ln}" for i, ln in enumerate(shown))
        if len(lines) > len(shown):
            body += f"\n... [{len(lines) - len(shown)} more lines, raise max_lines] ..."
        return body or "(empty file)"

    @tool(name="list_dir",
          description="List a project directory: name, type and size.")
    def list_dir(path: str = ".") -> str:
        """path: project-relative directory (default: project root)."""
        try:
            p = _resolve(path)
        except PermissionError as exc:
            return f"ERROR: {exc}"
        if not p.is_dir():
            return f"ERROR: not a directory: {path}"
        rows = []
        for entry in sorted(p.iterdir())[:MAX_LIST_ENTRIES]:
            if entry.name in BLOCKED_DIRS:
                continue
            try:
                size = entry.stat().st_size if entry.is_file() else 0
            except OSError:
                size = 0
            rows.append(f"{'d' if entry.is_dir() else 'f'}  {size:>10}  {entry.name}")
        return "\n".join(rows) or "(empty)"

    @tool(name="write_file",
          description=(f"Create or overwrite a file inside your scope {scope}. "
                       f"Writes outside it are refused."))
    def write_file(path: str, content: str) -> str:
        """path: project-relative path. content: full file contents."""
        bad = scope_violation(path, agent)
        if bad:
            return f"REFUSED: {bad}"
        try:
            p = _resolve(path)
        except PermissionError as exc:
            return f"REFUSED: {exc}"
        parts = p.relative_to(PROJECT_ROOT.resolve()).parts
        if not parts or parts[0] not in scope:
            return f"REFUSED: {agent} may only write inside {scope}; got '{path}'"
        if p.exists() and p.is_dir():
            return f"REFUSED: {path} is a directory"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"WROTE {len(content)} chars to {path}"

    @tool(name="run_shell",
          description=("Run a shell command in the project root and return its output. "
                       "Use for dotnet, git, unity CLI. Destructive commands are refused."))
    async def run_shell(command: str, timeout: int = SHELL_TIMEOUT) -> str:
        """command: the command line. timeout: seconds before it is killed."""
        why = command_violation(command)
        if why:
            return f"BLOCKED: {why}. Command not executed:\n{command[:200]}"
        bad = scope_violation(command, agent)
        if bad:
            return f"BLOCKED: cross-scope write - {bad}. Command not executed."
        proc = await asyncio.create_subprocess_shell(
            command, cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"TIMEOUT after {timeout}s: {command}"
        text = (out or b"").decode("utf-8", errors="replace").strip()
        header = f"exit={proc.returncode}\n"
        return header + (clip_output(text) or "(no output)")

    @tool(name="log_progress",
          description=("Append a short progress note (done / in progress / next) to your "
                       "PORT_LOG so a restart can resume. Call after each finished item."))
    def log_progress(note: str) -> str:
        """note: 1-3 lines describing where you are."""
        p = PROJECT_ROOT / f"PORT_LOG_{agent}.md"
        try:
            with p.open("a", encoding="utf-8") as fh:
                fh.write(f"\n- {time.strftime('%Y-%m-%d %H:%M')} {note.strip()[:1500]}\n")
        except OSError as exc:
            return f"ERROR: {exc}"
        return f"LOGGED to {p.name}"

    return [read_file, list_dir, write_file, run_shell, log_progress]


# --------------------------------------------------------------------------
# Request budget + retry, as MAF chat middleware
# --------------------------------------------------------------------------

class BudgetExhausted(Exception):
    """The request budget is used up. Never retried."""


class FatalModelError(Exception):
    """Auth or configuration error; retrying will not fix it."""


@dataclass
class Budget:
    limit: int = MAX_REQUESTS
    used: int = 0
    path: Path | None = None
    exhausted: bool = False
    fatal: str | None = None
    events: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def spend(self, n: int = 1) -> None:
        if self.used + n > self.limit:
            self.exhausted = True
            raise BudgetExhausted(f"request budget exhausted ({self.used}/{self.limit})")
        self.used += n
        if self.path is not None:
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps({"used": self.used, "limit": self.limit}),
                               encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass


def classify_error(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    if status in (401, 403, 404):
        return "fatal"
    if status in (400, 413, 422):
        return "reject"
    return "retry"


def budget_middleware(budget: Budget, on_event=None):
    """Chat middleware: count every attempt, retry transient errors with backoff."""
    from agent_framework import ChatContext

    async def middleware(context: ChatContext, call_next) -> None:
        attempt = 0
        while True:
            budget.spend(1)
            try:
                await call_next()
                return
            except BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                kind = classify_error(exc)
                limit = 3 if kind == "fatal" else 2 if kind == "reject" else 6
                if attempt >= limit:
                    if kind == "fatal":
                        budget.fatal = f"{type(exc).__name__}: {exc}"
                        raise FatalModelError(budget.fatal) from exc
                    raise
                delay = min(300.0, 5.0 * (2 ** (attempt - 1)))
                if on_event:
                    on_event(f"model error ({kind}) {type(exc).__name__}: "
                             f"{str(exc)[:140]} - retry {attempt} in {delay:.0f}s "
                             f"[{budget.used}/{budget.limit}]")
                await asyncio.sleep(delay)

    return middleware
