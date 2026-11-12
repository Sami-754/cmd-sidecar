# Command Code Sidecar API (`cmd-service`)

OpenAI-compatible local inference bridge powered by Command Code (`cmd`).

> **OS Support: Linux only.** Relies on `systemd --user`, `nice -n 19`, and Linux process management. Windows is untested, unsupported, and not planned.

## Connection Parameters
* **Host Base URL:** `http://localhost:8001/v1`
* **Docker / n8n Base URL:** `http://host.docker.internal:8001/v1`
* **API Key:** `sk-cmd-local` *(or any key matching `sk-cmd*`)*
* **Default Model:** `deepseek/deepseek-v4.1-flash`

## Supported Models (98 Total)
Discovered dynamically from `cmd --list-models`. Highlights:
* `deepseek/deepseek-v4.1-flash` (Default)
* `deepseek/deepseek-v4-flash`
* `moonshotai/kimi-k3`
* `moonshotai/kimi-k3.1-pro`
* `z-ai/glm-5.3-flash`
* `qwen/qwen3.8-flash`
* `qwen/qwen3.8-max`
* `google/gemini-3.8-flash`
* `claude-sonnet-5-5`
* `gpt-4o` / `gpt-4o-mini` (Aliases mapped directly to `deepseek/deepseek-v4.1-flash`)

## Quickstart Examples

### cURL (Streaming)
```bash
curl -N -X POST http://localhost:8001/v1/chat/completions \
  -H "Authorization: Bearer sk-cmd-local" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek/deepseek-v4.1-flash",
    "messages": [{"role": "user", "content": "Count from 1 to 5"}],
    "stream": true
  }'
```

### Python SDK
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8001/v1",
    api_key="sk-cmd-local"
)

response = client.chat.completions.create(
    model="deepseek/deepseek-v4.1-flash",
    messages=[{"role": "user", "content": "Explain vector embeddings in 1 sentence."}]
)
print(response.choices[0].message.content)
```

## Systemd Daemon Management
* **Unit File:** `~/.config/systemd/user/cmd-sidecar.service`
* **Source:** `server.py`
* **Tool Scrubber Mod:** `no-tools.mjs`
* **Restart:** `systemctl --user restart cmd-sidecar.service`
