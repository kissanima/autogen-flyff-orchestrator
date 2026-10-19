"""AutoGen installation smoke test - offline, no API key required."""

import asyncio
import importlib.metadata as md

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_ext.models.replay import ReplayChatCompletionClient
from autogen_ext.tools.mcp import McpWorkbench, StdioServerParams

PACKAGES = [
    "autogen-agentchat",
    "autogen-core",
    "autogen-ext",
    "mcp",
    "openai",
]

for pkg in PACKAGES:
    print(f"{pkg:20} {md.version(pkg)}")
print("OK: full import chain resolves\n")


async def main() -> None:
    # Replay client = canned responses, so this runs with no network/API key.
    client = ReplayChatCompletionClient(
        [
            '{"content": "Agent A here."}',
            '{"content": "Agent B here."}',
            '{"content": "Wrapping up."}',
        ]
        * 10
    )

    agent_a = AssistantAgent("A", model_client=client)
    agent_b = AssistantAgent("B", model_client=client)

    team = RoundRobinGroupChat(
        [agent_a, agent_b],
        termination_condition=MaxMessageTermination(5),
    )
    print("OK: team constructed ->", type(team).__name__)
    print("    agents:", [a.name for a in (agent_a, agent_b)])

    result = await team.run()
    print(f"OK: team ran, stop reason = {result.stop_reason}")

    # Confirm MCP wiring types are constructible against the real config shape.
    for name, params in {
        "rea": StdioServerParams(
            command="npx",
            args=["-y", "rea-agents@6.4.0", "mcp"],
        ),
        "unity": StdioServerParams(command="unity", args=["mcp"]),
    }.items():
        workbench = McpWorkbench(params)
        print(f"OK: McpWorkbench for '{name}' -> command={params.command!r}")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())