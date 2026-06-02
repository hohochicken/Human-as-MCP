"""
Entry point for the HumanMCP server.

Usage:
    python server/main.py
    python -m server.main
"""

import asyncio
import sys
from pathlib import Path

# Ensure the project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


async def main():
    """Start the HumanMCP server."""
    from server.app import init, create_app

    # Initialise storage, run migrations, verify integrity.
    await init()

    # Build the FastMCP app and register HTTP routes.
    app = create_app()

    print()
    print("=" * 62)
    print("  HumanMCP Server")
    print("  MCP Endpoint : http://127.0.0.1:4350/mcp")
    print("  Dashboard    : http://127.0.0.1:4350/dashboard")
    print("  Health Check : http://127.0.0.1:4350/health")
    print("=" * 62)
    print()
    print("Press Ctrl+C to stop.")
    print()

    app.run(transport="streamable-http", host="127.0.0.1", port=4350)


if __name__ == "__main__":
    asyncio.run(main())
