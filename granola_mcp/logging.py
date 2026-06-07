"""Shared utilities for MCP servers."""

from mcp.server.fastmcp import Context
from datetime import datetime


class DualLogger:
    """Logs messages to both stdout and MCP client context."""

    def __init__(self, ctx: Context):
        self.ctx = ctx

    def _timestamp(self) -> str:
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    async def info(self, msg: str):
        print(f'[{self._timestamp()}] [INFO] {msg}')
        await self.ctx.info(msg)

    async def debug(self, msg: str):
        print(f'[{self._timestamp()}] [DEBUG] {msg}')
        await self.ctx.debug(msg)

    async def warning(self, msg: str):
        print(f'[{self._timestamp()}] [WARNING] {msg}')
        await self.ctx.warning(msg)

    async def error(self, msg: str):
        print(f'[{self._timestamp()}] [ERROR] {msg}')
        await self.ctx.error(msg)
