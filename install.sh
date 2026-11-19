#!/usr/bin/env bash
# Idempotent installer for cmd-sidecar: provisions the venv, a working node, the systemd
# user unit, and the pi provider block. Safe to re-run on any device.
set -euo pipefail

SERVICE_NAME="cmd-sidecar"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${CMD_PORT:-8001}"
API_KEY="${CMD_API_KEY:-sk-cmd-local}"
PI_MODELS_JSON="${PI_MODELS_JSON:-$HOME/.pi/agent/models.json}"

echo "==> Setting up $SERVICE_NAME from $DIR..."

if ! command -v python3 &>/dev/null; then
    echo "Error: python3 is required but not installed." >&2
    exit 1
fi

# --- Node: pick one that actually executes ---------------------------------
# A distro node can be ABI-shifted by a library upgrade (e.g. libsimdjson.so.NN missing)
# and then every `#!/usr/bin/env node` CLI dies with exit 127. Probe before trusting PATH.
NODE_BIN=""
for candidate in "$(command -v node 2>/dev/null || true)" \
                 "$HOME/.local/share/fnm/aliases/default/bin/node" \
                 "$HOME/.nvm/versions/node/"*/bin/node \
                 "/usr/local/bin/node" "/usr/bin/node"; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    if "$candidate" -e 'process.exit(0)' >/dev/null 2>&1; then
        # Canonicalise: a version manager may hand us a volatile shell-scoped symlink
        # (/run/user/.../fnm_multishells/...) that vanishes after logout.
        NODE_BIN="$(readlink -f "$candidate")"
        break
    fi
done

EXTRA_PATH=""
if [ -n "$NODE_BIN" ]; then
    NODE_DIR="$(dirname "$NODE_BIN")"
    EXTRA_PATH="$NODE_DIR:"
    echo "==> Using node: $NODE_BIN ($("$NODE_BIN" -v 2>/dev/null || echo unknown))"
else
    echo "Warning: no working 'node' binary found. The cmd CLI will fail with exit 127." >&2
    echo "         Install Node (or fix the distro package) before using inference endpoints." >&2
fi
EXTRA_PATH="${EXTRA_PATH}$HOME/.npm-global/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

# --- cmd CLI ---------------------------------------------------------------
CMD_BIN="${CMD_BINARY:-}"
if [ -z "$CMD_BIN" ]; then
    CMD_BIN="$(PATH="$EXTRA_PATH" command -v cmd 2>/dev/null || true)"
fi
if [ -n "$CMD_BIN" ]; then
    echo "==> Found cmd CLI: $CMD_BIN"
else
    echo "Warning: 'cmd' not found. Install Command Code: npm install -g command-code" >&2
fi

# --- Python environment ----------------------------------------------------
PYTHON_BIN=""
if command -v python3 -m venv &>/dev/null && python3 -m venv "$DIR/.venv" 2>/dev/null; then
    echo "==> Installing Python dependencies into .venv..."
    "$DIR/.venv/bin/python3" -m pip install --quiet --upgrade pip 2>/dev/null || true
    "$DIR/.venv/bin/python3" -m pip install --quiet -r "$DIR/requirements.txt" 2>/dev/null \
        || "$DIR/.venv/bin/python3" -m pip install --quiet fastapi uvicorn pydantic
    PYTHON_BIN="$DIR/.venv/bin/python3"
else
    PYTHON_BIN="$(command -v python3)"
    if ! "$PYTHON_BIN" -c "import fastapi, uvicorn, pydantic" 2>/dev/null; then
        "$PYTHON_BIN" -m pip install --break-system-packages --quiet fastapi uvicorn pydantic 2>/dev/null \
            || "$PYTHON_BIN" -m pip install --user --quiet fastapi uvicorn pydantic 2>/dev/null || true
    fi
fi

# --- pi provider block (models are filled in dynamically by the service) ----
if [ -d "$(dirname "$PI_MODELS_JSON")" ]; then
    "$PYTHON_BIN" - "$PI_MODELS_JSON" "$PORT" "$API_KEY" <<'PY'
import json, os, sys

path, port, api_key = sys.argv[1], sys.argv[2], sys.argv[3]
data = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh) or {}
provider = data.setdefault("providers", {}).setdefault("cmd", {
    "name": f"Command Code cmd sidecar (localhost:{port})",
    "baseUrl": f"http://localhost:{port}/v1",
    "api": "openai-completions",
    "apiKey": api_key,
    "compat": {"supportsUsageInStreaming": False},
    "models": [],
})
provider.setdefault("models", [])
with open(path + ".tmp", "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
os.replace(path + ".tmp", path)
print(f"==> pi provider 'cmd' registered in {path} (models synced by the service at boot)")
PY
else
    echo "==> Skipping pi config: $(dirname "$PI_MODELS_JSON") does not exist"
fi

# --- systemd user unit -----------------------------------------------------
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat <<EOF > "$UNIT_DIR/$SERVICE_NAME.service"
[Unit]
Description=Command Code cmd Sidecar API Service
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$PYTHON_BIN -m uvicorn server:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5
Environment=PATH=$EXTRA_PATH
Environment=HOME=$HOME
Environment=CMD_API_KEY=$API_KEY
Environment=PI_MODELS_JSON=$PI_MODELS_JSON
Environment=PI_MODELS_SYNC=${PI_MODELS_SYNC:-1}
Environment=PI_MODELS_SYNC_INTERVAL=${PI_MODELS_SYNC_INTERVAL:-300}
Environment=PI_MODELS_DENY=${PI_MODELS_DENY:-kimi,moonshot,qwen}

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME.service"
systemctl --user restart "$SERVICE_NAME.service"

echo "==> Verifying service on port $PORT..."
if command -v curl &>/dev/null; then
    if curl -sf --retry 15 --retry-delay 1 --retry-connrefused "http://localhost:$PORT/health" | grep -q '"status"'; then
        echo "==> Success! $SERVICE_NAME is running on port $PORT"
        echo "    Host Base URL:   http://localhost:$PORT/v1"
        echo "    Docker Base URL: http://host.docker.internal:$PORT/v1"
        echo "    API Key:         $API_KEY"
        sync_out="$(curl -sf -X POST "http://localhost:$PORT/v1/models/sync" -H "Authorization: Bearer $API_KEY" || true)"
        if [ -n "$sync_out" ]; then
            echo "==> Catalog synced: $sync_out"
        fi
    else
        echo "Service started, but health check did not respond yet. Check: journalctl --user -u $SERVICE_NAME -n 25"
    fi
else
    echo "Service started. Check logs: journalctl --user -u $SERVICE_NAME -n 25"
fi
