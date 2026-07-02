"""
MCP handshake test — launches mcp_server.py as a real subprocess against a
fixture vault and drives it over stdio like Claude Desktop would.

This is the test that would have caught the shipped v2 server: undefined
`encrypted`, wrong InitializationOptions import, and stdout banners corrupting
the JSON-RPC channel all cause this to fail.
"""

import os
import sys
import asyncio
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_vault_core import _write_fixture_vault  # reuse the fixture builder

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession


async def _drive():
    tmp = tempfile.mkdtemp()
    vault, lic = _write_fixture_vault(tmp, compress=True)

    env = dict(os.environ)
    env["ICT_VAULT_FILE"] = str(vault)
    env["ICT_VAULT_LICENSE"] = str(lic)

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "scripts" / "mcp_server.py")],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.serverInfo.name == "ict-knowledge-vault"

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {"search_ict", "list_playlists", "explore_concept",
                    "vault_stats", "glossary_lookup"} <= names
            assert len(tools.tools) == 5

            stats = await session.call_tool("vault_stats", {})
            text = stats.content[0].text
            assert "Licensed to: tester@example.com" in text

            res = await session.call_tool("search_ict", {"query": "fair value gap"})
            assert "Fair Value Gap Explained" in res.content[0].text

            gl = await session.call_tool("glossary_lookup", {"term": "FVG"})
            assert "Fair Value Gap" in gl.content[0].text


def test_mcp_handshake_and_tools():
    asyncio.run(asyncio.wait_for(_drive(), timeout=60))
