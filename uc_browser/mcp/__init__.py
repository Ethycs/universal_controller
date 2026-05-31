"""MCP tool registrations for UCBrowser-driven sites.

Each module exposes a ``register_*_tools(server)`` function that attaches
tools to an existing FastMCP server. Designed so a host MCP server (e.g.
event-harvester's) can opt in to specific site toolsets via a one-line
import + call.
"""
