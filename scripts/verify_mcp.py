"""Verify the MCP server against a real MCP client over stdio.

Brief section 3.2.3 requires the server to be verified with an MCP client rather
than merely written. This launches the server as a subprocess, performs the
protocol handshake, lists the tools, and calls one that needs no API key, then
checks that what comes back is the deterministic core's output unchanged.

Run:  python scripts/verify_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from materials_trust import hubbard

EXPECTED_TOOLS = {
    "audit_material_tool",
    "compare_sources_tool",
    "check_physics_consistency_tool",
    "get_provenance_tool",
    "explain_hubbard_policy_tool",
}


def _text_of(result) -> str:
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


async def run() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "materials_trust.mcp_server"],
    )
    failures: list[str] = []

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            server_name = getattr(getattr(init, "serverInfo", None), "name", "unknown")
            print(f"handshake ok, server reports name: {server_name}")

            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            print(f"tools advertised: {sorted(names)}")
            missing = EXPECTED_TOOLS - names
            if missing:
                failures.append(f"tools missing from the server: {sorted(missing)}")

            for tool in listed.tools:
                if not (tool.description or "").strip():
                    failures.append(f"tool {tool.name} has no description")

            # A tool that needs no API key and no network, so this verifies the
            # transport and the payload shape rather than API availability.
            print("\ncalling explain_hubbard_policy_tool(formula='FeF3')")
            result = await session.call_tool(
                "explain_hubbard_policy_tool", {"formula": "FeF3"}
            )
            payload = json.loads(_text_of(result))
            print(json.dumps(payload, indent=2)[:900])

            # The response must equal what the core produces directly. This is the
            # check that layer 2 recomputes nothing.
            direct = hubbard.compare_hubbard_treatment("FeF3").to_dict()
            if payload != direct:
                failures.append(
                    "the MCP response differs from the core's direct output, which "
                    "means layer 2 is transforming values rather than passing them "
                    "through"
                )
            else:
                print("\nresponse is byte-identical to the core's direct output")

            if "Fe" not in (payload.get("only_mp") or {}):
                failures.append(
                    "expected FeF3 to show Fe receiving +U at Materials Project only"
                )

            print("\ncalling get_provenance_tool with a bad source, expecting a "
                  "structured error rather than a crash")
            bad = await session.call_tool(
                "get_provenance_tool", {"source": "nonsense", "identifier": "TiO2"}
            )
            bad_payload = json.loads(_text_of(bad))
            if bad_payload.get("error") != "bad_argument":
                failures.append(f"expected a bad_argument error, got {bad_payload}")
            else:
                print(f"  handled: {bad_payload['detail'][:110]}")

    print()
    if failures:
        for f in failures:
            print(f"FAILED: {f}", file=sys.stderr)
        return 1
    print("MCP server verified against a live client.")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
