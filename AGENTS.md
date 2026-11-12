# AGENTS.md — Command Code Sidecar (`cmd-service`)

Local OpenAI-compatible API daemon backed by the official Command Code CLI binary (`cmd`).

## Operational Directives
* **Binary Path:** Resolved automatically via `shutil.which("cmd")` or `~/.npm-global/bin/cmd` (or `CMD_BINARY` environment variable).
* **Headless Contract:** Always invoke in headless print mode with:
  `-p <prompt> -m <model> --no-session --no-skills --skip-onboarding --mod no-tools.mjs --output-format json`
* **Raw Model Enforcement:** `no-tools.mjs` MUST remain loaded. It calls `cmd.setActiveTools([])` to scrub function definitions from the model's schema and strips out the coding agent system prompt bloat.
* **Zero Lag Policy:** Any CLI spawn MUST maintain `nice -n 19` and lock behind `asyncio.Semaphore(1)`. Never increase concurrency beyond 1.
* **Port Binding:** `0.0.0.0:8001` (dual access for `localhost` and Docker `host.docker.internal`).

## Endpoints
* `GET /health` — Service health and binary metadata.
* `GET /v1/models` — Dynamically discovers and returns all 98 models available in `cmd --list-models`.
* `POST /v1/chat/completions` — OpenAI Chat Completions (supports streaming SSE + non-streaming JSON).
* `POST /v1/responses` — OpenAI Responses API (required by n8n OpenAI Node v2.3+).

## How to Customize or Edit Prompts

### Dynamic Prompting (Recommended)
All prompting is dynamic. The daemon does not inject hardcoded business logic or rules:
* Pass system prompts, instructions, few-shot examples, and personas directly in the caller's request payload via `/v1/chat/completions` or `/v1/responses`.
* In n8n, put task directives directly into the node's Prompt or System Message fields.

### Persona Override in Tool Scrubber
To change the baseline system persona used when stripping coding agent tools:
* Edit `no-tools.mjs` line 6 (`transformSystemPrompt`).
* Restart daemon: `systemctl --user restart cmd-sidecar.service`.

## Daemon Lifecycle
```bash
# Check status / logs
systemctl --user status cmd-sidecar.service
journalctl --user -u cmd-sidecar -f

# Restart daemon after edits
systemctl --user restart cmd-sidecar.service
```
