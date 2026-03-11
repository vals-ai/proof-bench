#!/usr/bin/env python3
"""Quick sanity check for lean_loogle with --loogle-local."""

import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

QUERIES = [
    {"label": "nat", "query": "Nat.add_comm", "max_results": 3},
    {"label": "bogus", "query": "bogus_identifier_that_should_fail", "max_results": 2},
]

LEAN_PROJECT_ROOT = next(
    (candidate for candidate in Path(__file__).resolve().parents if (candidate / "lean-toolchain").is_file()),
    None,
)
if LEAN_PROJECT_ROOT is None:
    raise RuntimeError("Unable to locate Lean project root (missing lean-toolchain file).")


async def _run_loogle_local_test() -> None:
    command = ["uvx", "lean-lsp-mcp", "--transport", "stdio", "--loogle-local"]
    params = StdioServerParameters(
        command=command[0],
        args=command[1:],
        env={
            "LEAN_PROJECT_PATH": str(LEAN_PROJECT_ROOT),
            "LEAN_LOOGLE_LOCAL": "true",
        },
    )

    async with AsyncExitStack() as stack:
        transport = await stack.enter_async_context(stdio_client(params))
        session = ClientSession(read_stream=transport[0], write_stream=transport[1])
        await stack.enter_async_context(session)
        await session.initialize()

        for spec in QUERIES:
            print(f"[{spec['label']}] querying lean_loogle...")
            result = await session.call_tool(
                name="lean_loogle",
                arguments={"query": spec["query"], "max_results": spec["max_results"]},
            )

            if result.isError:
                print(f"[{spec['label']}] error: {getattr(result, 'message', 'unknown error')}")
                for item in result.content or []:
                    print(json.dumps(item.model_dump()))
                continue

            for item in result.content:
                text = getattr(item, "text", None)
                if text is not None:
                    print(f"[{spec['label']}] {text.strip()}")
                else:
                    print(f"[{spec['label']}] {json.dumps(item.model_dump())}")


def test_loogle_local() -> None:
    asyncio.run(_run_loogle_local_test())


if __name__ == "__main__":
    asyncio.run(_run_loogle_local_test())
