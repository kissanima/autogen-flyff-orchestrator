"""MCP connectivity test: can AutoGen's MCP layer reach both configured servers?

Connects to each stdio MCP server from ~/.workbuddy-ai/mcp.json, lists its tools,
and disconnects. No agents, no LLM calls.
"""

import asyncio
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MCP_JSON = Path.home() / ".workbuddy-ai" / "mcp.json"


async def probe(name: str, params: StdioServerParameters, timeout: int) -> None:
    print(f"\n{'=' * 60}\n[{name}] command={params.command} {params.args}\n{'=' * 60}")
    try:
        async with asyncio.timeout(timeout):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    print(f"  CONNECTED · {len(tools.tools)} tools")
                    for t in tools.tools[:15]:
                        desc = (t.description or "")[:70].replace("\n", " ")
                        print(f"    - {t.name}: {desc}")
                    if len(tools.tools) > 15:
                        print(f"    ... ({len(tools.tools) - 15} more)")
    except asyncio.TimeoutError:
        print(f"  TIMEOUT after {timeout}s (server hung or still starting)")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")


async def main() -> None:
    cfg = json.loads(MCP_JSON.read_text(encoding="utf-8"))["mcpServers"]
    await probe("rea", StdioServerParameters(
        command=cfg["rea"]["command"], args=cfg["rea"]["args"]), timeout=180)
    await probe("unity", StdioServerParameters(
        command=cfg["unity"]["command"], args=cfg["unity"]["args"]), timeout=60)


if __name__ == "__main__":
    asyncio.run(main())
