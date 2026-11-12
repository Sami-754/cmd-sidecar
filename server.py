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
            if line in ("Open Source", "Stealth", "Anthropic", "OpenAI", "Google", "Sakana", "Meta", "xAI"):
                continue
            parts = re.split(r"\s{2,}", line, maxsplit=1)
            model_id = parts[0].strip()
            desc = parts[1].strip() if len(parts) > 1 else ""
            if ("/" in model_id or "-" in model_id) and model_id not in seen:
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

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: Optional[str] = "deepseek/deepseek-v4.1-flash"
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False

# --- Endpoints ---

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
    """OpenAI-compatible models catalog."""
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
        ]
    }

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(req: ChatCompletionRequest, _: bool = Depends(verify_api_key)):
    """OpenAI-compatible Chat Completions endpoint."""
    conversation = []
    for msg in req.messages:
        role_label = "System" if msg.role == "system" else ("User" if msg.role == "user" else "Assistant")
        conversation.append(f"{role_label}:\n{msg.content}")
    
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
