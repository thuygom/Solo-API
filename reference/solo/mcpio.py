"""Optional MCP adapter over the same registry. Official Python SDK, checked with mcp==2.3.0.

    SOLO_ACCOUNT_ID=<verified account> python -m solo.mcpio --db solo.db      # MCP stdio

The tools and their contracts do not depend on this file. It only translates tools/list and
tools/call. One stdio process serves one account, and the host sets that account in the
environment; a tool argument cannot change it.
"""
import argparse
import json
import os

import anyio
import mcp_types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .demo import Service

INSTRUCTIONS = (
    "계정은 서버가 정하며 입력으로 바꿀 수 없습니다. write 도구는 사용자 확인 뒤 호출합니다. "
    "Question 입력은 값을 모르면 먼저 사용자에게 묻습니다. "
    "오류의 error.next_action을 따르고, 실패를 빈 결과라고 설명하지 않습니다."
)


def build(tools, account_id):
    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[
            types.Tool(name=t["name"], title=t["title"], description=t["description"], input_schema=t["inputSchema"],
                       annotations=types.ToolAnnotations(
                           title=t["title"], read_only_hint=t["annotations"]["readOnlyHint"], destructive_hint=False,
                           idempotent_hint=t["annotations"]["idempotentHint"], open_world_hint=False))
            for t in tools.catalog("mcp")["tools"]])

    async def call_tool(ctx, params):
        arguments = params.arguments or {}
        # An MCP client sends no retry key; the registry's content window decides what counts as a retry.
        out = await anyio.to_thread.run_sync(lambda: tools.call(params.name, arguments, account_id=account_id))
        return types.CallToolResult(content=[types.TextContent(text=json.dumps(out, ensure_ascii=False))],
                                    structured_content=out, is_error=not out["ok"])

    return Server("solo-api-demo", version="0.1.0", title="Solo-API demo", instructions=INSTRUCTIONS,
                  on_list_tools=list_tools, on_call_tool=call_tool)


CONTENT_WINDOW = 600  # seconds: the same write input within 10 minutes is a retry, after that a new intent


async def serve(db, account_id):
    server = build(Service(db, content_window=CONTENT_WINDOW).tools, account_id)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="solo.mcpio")
    parser.add_argument("--db", default="solo.db")
    args = parser.parse_args(argv)
    account_id = os.environ.get("SOLO_ACCOUNT_ID")
    if not account_id:
        raise SystemExit("SOLO_ACCOUNT_ID must be set by the host to the verified account")
    anyio.run(serve, args.db, account_id)


if __name__ == "__main__":
    main()
