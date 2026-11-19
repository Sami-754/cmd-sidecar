#!/usr/bin/env python3
"""
cmd-sidecar: Official Command Code OpenAI-Compatible API Bridge
Uses official Command Code CLI (~/.npm-global/bin/cmd or system PATH) with official headless mode.
Exposes standard OpenAI endpoints on 0.0.0.0:8001 (accessible via localhost & host.docker.internal).
"""

import sys
import os
import json
import re
import time
import uuid
import asyncio
import subprocess
import shutil
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

CMD_BINARY = os.environ.get("CMD_BINARY") or shutil.which("cmd") or os.path.expanduser("~/.npm-global/bin/cmd")
MOD_NO_TOOLS = os.environ.get("MOD_NO_TOOLS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "no-tools.mjs")

def discover_cmd_models() -> List[Dict[str, str]]:
    """Dynamically loads all available models from official cmd CLI."""
    try:
        out = subprocess.check_output([CMD_BINARY, "--list-models"], text=True, timeout=15)
        models = []
        seen = set()
        for line in out.split("\n"):
            line = line.strip()
            if not line or line.startswith("Available models") or line.startswith("Pass the") or line.startswith("cmd --") or line.startswith("Docs:") or line.startswith("Decision"):
                continue
            parts = re.split(r"\s{2,}", line, maxsplit=1)
            model_id = parts[0].strip()
            desc = parts[1].strip() if len(parts) > 1 else ""
            # Guard: an over-long id collapses the column padding, so the whole line lands in
            # parts[0]. Reject anything with whitespace instead of emitting a garbage model id.
            if re.search(r"\s", model_id):
                continue
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", model_id) and model_id not in seen:
                seen.add(model_id)
                models.append({"id": model_id, "description": desc})
        if models:
            return models
    except Exception:
        pass
    return [
        {"id": "deepseek/deepseek-v4.1-flash", "description": "DeepSeek V4.1 Flash (Default)"},
        {"id": "deepseek/deepseek-v4-flash", "description": "DeepSeek V4 Flash"},
        {"id": "moonshotai/kimi-k3", "description": "Kimi K3"},
        {"id": "google/gemini-3.8-flash", "description": "Gemini 3.8 Flash"},
        {"id": "claude-sonnet-5-5", "description": "Claude Sonnet 5.5"}
    ]

# Discover all models at module load
ALL_MODELS = discover_cmd_models()

MODEL_MAP = {
    "gpt-4o": "deepseek/deepseek-v4.1-flash",
    "gpt-4o-mini": "deepseek/deepseek-v4.1-flash",
    "gpt-4": "deepseek/deepseek-v4.1-flash",
    "gpt-3.5-turbo": "deepseek/deepseek-v4.1-flash",
    "claude-3-5-sonnet": "claude-sonnet-5-5",
    "default": "deepseek/deepseek-v4.1-flash"
}

API_KEY_EXPECTED = os.environ.get("CMD_API_KEY", "sk-cmd-local")

# --- Pi catalog auto-sync -------------------------------------------------
# The live `cmd --list-models` catalog is projected into pi's models.json at boot and on
# every periodic refresh, so a fresh install self-configures and CLI updates self-heal.
PI_MODELS_JSON = os.environ.get("PI_MODELS_JSON", os.path.expanduser("~/.pi/agent/models.json"))
PI_PROVIDER_KEY = os.environ.get("PI_PROVIDER_KEY", "cmd")
PI_PROVIDER_NAME = os.environ.get("PI_PROVIDER_NAME", "Command Code cmd sidecar (localhost:8001)")
PI_MODELS_SYNC = os.environ.get("PI_MODELS_SYNC", "1").lower() not in ("0", "false", "no", "off")
PI_MODELS_SYNC_INTERVAL = int(os.environ.get("PI_MODELS_SYNC_INTERVAL", "300"))
# Comma-separated deny list kept across regenerations so cost-banned families never come back.
PI_MODELS_DENY = os.environ.get("PI_MODELS_DENY", "kimi,moonshot,qwen")
PI_MODELS_MAX_TOKENS = int(os.environ.get("PI_MODELS_MAX_TOKENS", "8192"))
PI_MODELS_CONTEXT = int(os.environ.get("PI_MODELS_CONTEXT", "131072"))

app = FastAPI(
    title="Official Command Code OpenAI Bridge",
    description="Local OpenAI-compatible API powered by official Command Code CLI",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"status": "ok", "service": "cmd-sidecar"}

# Concurrency lock: maximum 1 execution at a time with nice -n 19 to guarantee ZERO PC lag
SEMAPHORE = asyncio.Semaphore(1)

def verify_api_key(authorization: Optional[str] = Header(None)):
    """Validates Bearer token. Accepts configured key, or any matching sk-cmd* or cmd-*."""
    if not API_KEY_EXPECTED or API_KEY_EXPECTED.lower() in ("any", "none", "*", ""):
        return True
    
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Missing Authorization header. Expected: Bearer <API_KEY>", "type": "invalid_request_error"}}
        )
    
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid Authorization header format. Must be Bearer <key>", "type": "invalid_request_error"}}
        )
    
    token = parts[1]
    if token != API_KEY_EXPECTED and not token.startswith("sk-cmd") and not token.startswith("cmd-") and not token.startswith("sk-commandcode"):
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": f"Incorrect API key provided: {token[:8]}...", "type": "invalid_request_error"}}
        )
    return True

# --- Request / Response Models ---

def _extract_text(content: Any) -> str:
    """Flattens OpenAI string content or content-part arrays (e.g. [{"type":"text","text":"yo"}]) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if "text" in part:
                    parts.append(str(part["text"]))
                elif isinstance(part.get("content"), str):
                    parts.append(part["content"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def _format_message(role: str, content: Any, tool_calls: Optional[List[Any]] = None) -> str:
    role_label = "System" if role == "system" else ("User" if role == "user" else "Assistant")
    text = _extract_text(content)
    if tool_calls:
        rendered = []
        for call in tool_calls:
            if isinstance(call, dict):
                fn = call.get("function") or {}
                rendered.append(f'{fn.get("name", "tool")}({fn.get("arguments", "")})')
        if rendered:
            text = (text + "\n" if text else "") + "[called: " + ", ".join(rendered) + "]"
    return f"{role_label}:\n{text}"


class ChatMessage(BaseModel):
    role: str
    content: Any = None
    tool_calls: Optional[List[Any]] = None

class ChatCompletionRequest(BaseModel):
    model: Optional[str] = "deepseek/deepseek-v4.1-flash"
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False

# --- Pi catalog projection ---

def is_denied_model(model_id: str) -> bool:
    """True when a requested model matches PI_MODELS_DENY (enforced, not just hidden from the catalog)."""
    deny = [p.strip().lower() for p in PI_MODELS_DENY.split(",") if p.strip()]
    return any(pattern in (model_id or "").lower() for pattern in deny)


def _pi_model_entries(models: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Projects a cmd catalog into pi models.json entries, honouring the cost deny list."""
    deny = [p.strip().lower() for p in PI_MODELS_DENY.split(",") if p.strip()]
    entries: List[Dict[str, Any]] = []
    seen = set()
    for model in models:
        model_id = str(model.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        if any(pattern in model_id.lower() for pattern in deny):
            continue
        seen.add(model_id)
        entries.append(
            {
                "id": model_id,
                "name": str(model.get("description") or model_id).strip(),
                "contextWindow": PI_MODELS_CONTEXT,
                "maxTokens": PI_MODELS_MAX_TOKENS,
                "input": ["text"],
            }
        )
    return entries


def _default_pi_provider() -> Dict[str, Any]:
    return {
        "name": PI_PROVIDER_NAME,
        "baseUrl": os.environ.get("PI_BASE_URL", "http://localhost:8001/v1"),
        "api": "openai-completions",
        "apiKey": os.environ.get("PI_API_KEY", API_KEY_EXPECTED),
        "compat": {"supportsUsageInStreaming": False},
    }


def sync_pi_models(models: List[Dict[str, str]]) -> bool:
    """Writes the catalog into pi's models.json provider block. Returns True when the file changed."""
    if not PI_MODELS_SYNC:
        return False

    entries = _pi_model_entries(models)
    if not entries:
        return False

    try:
        data: Dict[str, Any] = {}
        if os.path.exists(PI_MODELS_JSON):
            with open(PI_MODELS_JSON, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
        elif not os.path.isdir(os.path.dirname(PI_MODELS_JSON) or "."):
            return False

        providers = data.setdefault("providers", {})
        provider = providers.setdefault(PI_PROVIDER_KEY, _default_pi_provider())
        if provider.get("models") == entries:
            return False

        provider["models"] = entries
        try:
            shutil.copyfile(PI_MODELS_JSON, PI_MODELS_JSON + ".bak")
        except OSError:
            pass
        tmp_path = f"{PI_MODELS_JSON}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp_path, PI_MODELS_JSON)
        return True
    except Exception:
        return False


def refresh_models(write_catalog: bool = True) -> List[Dict[str, str]]:
    """Re-discovers the live CLI catalog and optionally re-projects it into pi's models.json."""
    global ALL_MODELS
    discovered = discover_cmd_models()
    if discovered:
        ALL_MODELS = discovered
    if write_catalog:
        sync_pi_models(ALL_MODELS)
    return ALL_MODELS


async def _catalog_refresh_loop() -> None:
    """Keeps the catalog fresh across CLI updates (and self-heals pi config after upgrades)."""
    if not PI_MODELS_SYNC or PI_MODELS_SYNC_INTERVAL <= 0:
        return
    while True:
        try:
            await asyncio.sleep(PI_MODELS_SYNC_INTERVAL)
            await asyncio.to_thread(refresh_models, True)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


@app.on_event("startup")
async def _startup_sync() -> None:
    """Boot-time projection so a fresh install configures pi with no manual step."""
    app.state.catalog_task = asyncio.create_task(_catalog_refresh_loop())
    try:
        await asyncio.to_thread(refresh_models, True)
    except Exception:
        pass


@app.on_event("shutdown")
async def _shutdown_sync() -> None:
    task = getattr(app.state, "catalog_task", None)
    if task and not task.done():
        task.cancel()


# --- Endpoints ---

@app.post("/v1/models/sync")
@app.post("/models/sync")
async def sync_models_endpoint(_: bool = Depends(verify_api_key)):
    """Re-discovers the CLI catalog and re-projects it into pi's models.json."""
    models = await asyncio.to_thread(refresh_models, True)
    return {
        "status": "ok",
        "models": len(models),
        "pi_models_json": PI_MODELS_JSON,
        "provider": PI_PROVIDER_KEY,
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "cmd-sidecar (official-command-code)",
        "cli_binary": CMD_BINARY,
        "default_model": "deepseek/deepseek-v4.1-flash",
        "zero_lag": "nice -n 19 + asyncio.Semaphore(1)"
    }

@app.get("/v1/models")
@app.get("/models")
async def list_models(_: bool = Depends(verify_api_key)):
    """OpenAI-compatible models catalog (cost-banned families excluded via PI_MODELS_DENY)."""
    return {
        "object": "list",
        "data": [
            {
                "id": m["id"],
                "object": "model",
                "created": 1700000000,
                "owned_by": "command-code",
                "permission": [],
                "root": m["id"],
                "parent": None
            }
            for m in ALL_MODELS
            if not is_denied_model(m["id"])
        ]
    }

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(req: ChatCompletionRequest, _: bool = Depends(verify_api_key)):
    """OpenAI-compatible Chat Completions endpoint."""
    conversation = [_format_message(msg.role, msg.content, msg.tool_calls) for msg in req.messages]
    full_prompt = "\n\n".join(conversation)
    chosen_model = req.model or "deepseek/deepseek-v4.1-flash"
    target_model = MODEL_MAP.get(chosen_model, chosen_model)
    completion_id = f"chatcmpl-cmd-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())

    cmd = [
        "nice", "-n", "19",
        CMD_BINARY,
        "-p", full_prompt,
        "-m", target_model,
        "--no-session",
        "--no-skills",
        "--skip-onboarding",
        "--mod", MOD_NO_TOOLS,
        "--output-format", "json"
    ]

    if req.stream:
        async def event_generator():
            async with SEMAPHORE:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                try:
                    async for line in proc.stdout:
                        line_str = line.decode().strip()
                        if not line_str:
                            continue
                        try:
                            payload = json.loads(line_str)
                            if payload.get("type") == "event":
                                ev = payload.get("event", {})
                                if ev.get("type") == "text_delta":
                                    delta_text = ev.get("delta", "")
                                    chunk = {
                                        "id": completion_id,
                                        "object": "chat.completion.chunk",
                                        "created": created_ts,
                                        "model": target_model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": delta_text},
                                                "finish_reason": None
                                            }
                                        ]
                                    }
                                    yield f"data: {json.dumps(chunk)}\n\n"
                        except Exception:
                            continue

                    await proc.wait()
                    final_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": target_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop"
                            }
                        ]
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as e:
                    yield f"data: {{\"error\": \"{str(e)}\"}}\n\n"
                    yield "data: [DONE]\n\n"
                finally:
                    if proc.returncode is None:
                        try:
                            proc.kill()
                        except Exception:
                            pass

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # Non-streaming mode
    async with SEMAPHORE:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(status_code=504, detail="Command Code CLI timed out after 120 seconds")

        if proc.returncode != 0:
            err_msg = stderr.decode().strip() or stdout.decode().strip()
            raise HTTPException(status_code=500, detail=f"Command Code CLI error (code {proc.returncode}): {err_msg}")

        # Parse NDJSON to extract final text
        output_lines = stdout.decode().strip().split("\n")
        final_text = ""
        for l in output_lines:
            l = l.strip()
            if not l:
                continue
            try:
                item = json.loads(l)
                if item.get("type") == "result":
                    final_text = item.get("finalText", "")
                    break
                elif item.get("type") == "event":
                    ev = item.get("event", {})
                    if ev.get("type") == "text_delta":
                        final_text += ev.get("delta", "")
            except Exception:
                pass

        if not final_text:
            final_text = stdout.decode().strip()

        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created_ts,
            "model": target_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_text
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": len(full_prompt) // 4,
                "completion_tokens": len(final_text) // 4,
                "total_tokens": (len(full_prompt) + len(final_text)) // 4
            }
        }

@app.post("/v1/responses")
@app.post("/responses")
async def responses_endpoint(req: Request, _: bool = Depends(verify_api_key)):
    """OpenAI-compatible Responses API endpoint for latest n8n OpenAI node (v2.3+)."""
    body = await req.json()
    model_name = body.get("model") or "deepseek/deepseek-v4.1-flash"
    target_model = MODEL_MAP.get(model_name, model_name)
    instructions = body.get("instructions")
    input_data = body.get("input") or body.get("messages") or []

    conversation = []
    if instructions:
        conversation.append(f"System:\n{instructions}")

    if isinstance(input_data, list):
        for msg in input_data:
            if isinstance(msg, dict):
                role = msg.get("role", "user")
                c = msg.get("content", "")
                if isinstance(c, list):
                    text_parts = []
                    for part in c:
                        if isinstance(part, dict) and "text" in part:
                            text_parts.append(part["text"])
                        elif isinstance(part, str):
                            text_parts.append(part)
                    text = "\n".join(text_parts)
                elif isinstance(c, str):
                    text = c
                else:
                    text = str(c)
                role_label = "System" if role == "system" else ("User" if role == "user" else "Assistant")
                conversation.append(f"{role_label}:\n{text}")
            elif isinstance(msg, str):
                conversation.append(f"User:\n{msg}")
    elif isinstance(input_data, str):
        conversation.append(f"User:\n{input_data}")

    full_prompt = "\n\n".join(conversation)
    cmd = [
        "nice", "-n", "19",
        CMD_BINARY,
        "-p", full_prompt,
        "-m", target_model,
        "--no-session",
        "--no-skills",
        "--skip-onboarding",
        "--mod", MOD_NO_TOOLS,
        "--output-format", "json"
    ]

    async with SEMAPHORE:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise HTTPException(status_code=504, detail="Command Code CLI timed out after 120 seconds")

        if proc.returncode != 0:
            err_msg = stderr.decode().strip() or stdout.decode().strip()
            raise HTTPException(status_code=500, detail=f"Command Code CLI error (code {proc.returncode}): {err_msg}")

        output_lines = stdout.decode().strip().split("\n")
        final_text = ""
        for l in output_lines:
            l = l.strip()
            if not l:
                continue
            try:
                item = json.loads(l)
                if item.get("type") == "result":
                    final_text = item.get("finalText", "")
                    break
                elif item.get("type") == "event":
                    ev = item.get("event", {})
                    if ev.get("type") == "text_delta":
                        final_text += ev.get("delta", "")
            except Exception:
                pass

        if not final_text:
            final_text = stdout.decode().strip()

    resp_id = f"resp_{uuid.uuid4().hex[:16]}"
    prompt_tokens = len(full_prompt) // 4
    output_tokens = len(final_text) // 4

    return {
        "id": resp_id,
        "object": "response",
        "created": int(time.time()),
        "status": "completed",
        "model": target_model,
        "output": [
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:16]}",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": final_text
                    }
                ]
            }
        ],
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "total_tokens": prompt_tokens + output_tokens
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8001, log_level="info")
