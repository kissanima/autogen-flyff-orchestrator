"""Verify both configured models work: basic chat + tool calling.

Reads DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL from the environment (or .env).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

BASE = os.getenv("DEEPSEEK_BASE_URL", "https://www.aibh.cc/v1")
API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
if not API_KEY:
    sys.exit("DEEPSEEK_API_KEY is not set (put it in .env).")
MODEL_PRO = os.getenv("DEEPSEEK_PRO_MODEL", "deepseek-v4-pro")
MODEL_FLASH = os.getenv("DEEPSEEK_FLASH_MODEL", "deepseek-v4.1-flash")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add",
            "description": "Add two numbers",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
        },
    }
]


def post(model: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code, "body": e.read().decode()[:600]}
    except Exception as e:  # noqa: BLE001
        return {"__error__": f"{type(e).__name__}: {e}"}


for model in (MODEL_PRO, MODEL_FLASH):
    print(f"\n{'=' * 60}\n{model}\n{'=' * 60}")

    # 1. plain chat
    r = post(
        model,
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
            "max_tokens": 32,
        },
    )
    if "__http_error__" in r or "__error__" in r:
        print("  chat      : FAIL", r)
        continue
    msg = r["choices"][0]["message"]
    print(f"  chat      : OK -> {msg.get('content', '').strip()[:70]!r}")
    print(f"  reasoning : {bool(msg.get('reasoning_content'))}")

    # 2. tool calling (required for AutoGen agents)
    r2 = post(
        model,
        {
            "model": model,
            "messages": [
                {"role": "user", "content": "What is 17 + 25? Use the add tool."}
            ],
            "tools": TOOLS,
            "max_tokens": 256,
        },
    )
    if "__http_error__" in r2 or "__error__" in r2:
        print("  tool call : FAIL", r2)
        continue
    m2 = r2["choices"][0]["message"]
    calls = m2.get("tool_calls") or []
    if calls:
        fn = calls[0]["function"]
        print(f"  tool call : OK -> {fn['name']}({fn['arguments']})")
    else:
        print(f"  tool call : NO CALL -> content={m2.get('content', '')[:70]!r}")

print("\nDONE")