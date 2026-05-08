# syntax=docker/dockerfile:1
#
# This Dockerfile is provided so automated MCP directories (e.g. Glama) can
# spawn the server in a sandbox to verify that it starts and responds to
# tools/list introspection. It is NOT intended for actual use — the server
# reads Cursor's local SQLite at a Windows-only path (`%APPDATA%/Cursor/...`)
# which does not exist in the container, so tool *calls* will fail.
# Introspection alone is sufficient for the directory's checks.
#
# For real use, install on a Windows host via scripts/install.ps1.

FROM python:3.12-slim

WORKDIR /app

# Only runtime dependency: the MCP Python SDK
RUN pip install --no-cache-dir mcp

# Copy the server script
COPY scripts/server.py /app/server.py

# stdio transport: server reads JSON-RPC messages on stdin, writes on stdout
CMD ["python", "-u", "/app/server.py"]
