#!/usr/bin/env bash
# Run from a billing project containing mcp_server/server.py, or set
# BILLING_PROJECT_DIR to that project's root. Uses an existing environment.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${BILLING_PROJECT_DIR:-$SCRIPT_DIR}"

if [[ ! -f "$PROJECT_DIR/mcp_server/server.py" ]]; then
    echo "Set BILLING_PROJECT_DIR to your SDK-migrated billing project root, or place this script there." >&2
    exit 1
fi
cd -- "$PROJECT_DIR"

if [[ -n "${PYTHON_BIN:-}" ]]; then
    PYTHON="$PYTHON_BIN"
elif [[ -x ".venv/bin/python" ]]; then
    PYTHON=".venv/bin/python"
elif [[ -f ".venv/Scripts/python.exe" ]]; then
    PYTHON=".venv/Scripts/python.exe"
else
    PYTHON="python"
fi

export MCP_PORT="${MCP_PORT:-8010}"

# Importing the server skips its __main__ stdio runner and reuses all 12 tools.
# Bind locally: remote access needs a separately configured authenticated gateway.
exec "$PYTHON" -c '
import os
from mcp_server.server import mcp

port = int(os.environ["MCP_PORT"])
if not 1 <= port <= 65535:
    raise SystemExit("MCP_PORT must be between 1 and 65535.")
print(f"MCP endpoint: http://127.0.0.1:{port}/mcp", flush=True)
mcp.run(transport="http", host="127.0.0.1", port=port, path="/mcp", show_banner=False)
'
