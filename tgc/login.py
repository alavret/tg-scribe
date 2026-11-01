import asyncio

from mcp.server.mcpserver.exceptions import ToolError

from tgc.client import SESSION, telegram


async def main():
    async with telegram(login=True) as client:
        me = await client.get_me()
    SESSION.with_suffix(".session").chmod(0o600)
    print(f"Вход выполнен: {me.first_name} (@{me.username})")


try:
    asyncio.run(main())
except ToolError as e:
    raise SystemExit(str(e)) from None
