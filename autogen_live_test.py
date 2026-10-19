"""
Live AutoGen test against an OpenAI-compatible endpoint.

Reads configuration from .env (DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / model ids).
See autogen_providers.json for the model list.

Usage:
  python autogen_live_test.py [model_id]
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import (
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
)
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_ext.models.openai import OpenAIChatCompletionClient

PROVIDERS = Path(__file__).with_name("autogen_providers.json")


def build_client(model: str) -> OpenAIChatCompletionClient:
    cfg = json.loads(PROVIDERS.read_text(encoding="utf-8"))["aibh"]
    if model not in cfg["models"]:
        raise SystemExit(f"Unknown model {model!r}. Known: {sorted(cfg['models'])}")

    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("AIBH_API_KEY")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY is not set (put it in .env).")

    return OpenAIChatCompletionClient(
        model=model,
        api_key=key,
        base_url=cfg["base_url"],
        model_info={
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "family": "unknown",
            "structured_output": False,
        },
    )


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def flatten(content: object) -> str:
    """Message content can be a str or a list of content parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                out.append(part.get("text") or part.get("content") or "")
            else:
                out.append(str(part))
        return " ".join(x for x in out if x)
    return str(content)


async def run(model: str) -> None:
    print(f"\n{'=' * 62}\nLIVE AutoGen test -> {model}\n{'=' * 62}")

    client = build_client(model)

    worker = AssistantAgent(
        "calculator",
        model_client=client,
        tools=[add],
        system_message="You do arithmetic. Always use the add tool.",
    )
    writer = AssistantAgent(
        "writer",
        model_client=client,
        system_message="Summarise the calculation result in one short sentence.",
    )

    team = RoundRobinGroupChat(
        [worker, writer],
        termination_condition=MaxMessageTermination(4),
    )

    result = await team.run(
        task="Use the add tool to compute 17 + 25, then have the writer summarise it."
    )

    print(f"\nstop reason: {result.stop_reason}")
    print(f"messages  : {len(result.messages)}\n")
    for m in result.messages:
        src = getattr(m, "source", "?")
        content = flatten(m.content).strip().replace("\n", " ")
        print(f"  [{src}] {content[:110]}")

    executed = [m for m in result.messages if isinstance(m, ToolCallExecutionEvent)]
    requested = [m for m in result.messages if isinstance(m, ToolCallRequestEvent)]
    if executed:
        print(f"\ntool requested : {len(requested)} time(s)")
        for m in executed:
            print(f"tool executed  : {flatten(m.content)}")
    else:
        print("\nWARNING: no tool was executed")


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "deepseek-v4.1-flash"))