#!/bin/bash
# mcp-doctor: venv exists, `mcp` imports, bridge reachable on :9876.
set -u
VENV="${VENV:-$HOME/.venvs/fcp-mcp-lite}"
PORT="${BRIDGE_PORT:-9876}"
fail=0

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "[X] no venv at $VENV — run: make mcp-setup"; fail=1
else
  echo "[+] venv: $VENV"
  if ! "$VENV/bin/python" -c "import mcp" 2>/dev/null; then
    echo "[X] package 'mcp' not importable — run: make mcp-setup"; fail=1
  else
    echo "[+] 'mcp' imports cleanly"
  fi
fi

if ! python3 -c "import socket; socket.create_connection(('127.0.0.1',$PORT),timeout=3).close()" 2>/dev/null; then
  echo "[X] bridge not listening on 127.0.0.1:$PORT — launch patched FCP (make patch)"
  fail=1
else
  echo "[+] bridge reachable on 127.0.0.1:$PORT"
fi

exit $fail
