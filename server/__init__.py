"""Human-as-MCP server package.

Usage::

    python server/main.py

or::

    python -c "import asyncio; from server.app import init, create_app; asyncio.run(init()); create_app().run()"
"""

from server.app import mcp, create_app, init

__all__ = ["mcp", "create_app", "init"]
