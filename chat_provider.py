"""
chat_provider.py
----------------
Abstraction over LLM chat providers (Anthropic, Ollama cloud).

The beat-book writing agent, cluster-labeling pipeline, and ingest
normalization call through this interface so the same code works with
either backend.  The research agent is Anthropic-only and bypasses
this module.

Env vars (Ollama):
- CHAT_PROVIDER        "anthropic" (default) or "ollama"
- OLLAMA_CHAT_HOST     e.g. https://ollama.com  (cloud) or http://localhost:11434
- OLLAMA_CHAT_MODEL    e.g. qwen3:8b
- OLLAMA_API_KEY       Bearer token for Ollama cloud (omit for local)
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


RATE_LIMIT_MAX_RETRIES = 8


# ---------------------------------------------------------------------------
# Common response type
# ---------------------------------------------------------------------------

@dataclass
class ChatResponse:
    content: list[dict]
    stop_reason: str
    usage: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "".join(
            b["text"] for b in self.content if b.get("type") == "text"
        )

    @property
    def tool_calls(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class ChatProviderError(Exception):
    retry_after: Optional[float] = None


class ChatRateLimitError(ChatProviderError):
    pass


class ChatConnectionError(ChatProviderError):
    pass


class ChatServerError(ChatProviderError):
    pass


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class ChatProvider(Protocol):
    explore_model: str
    agent_model: str
    label_model: str
    normalize_model: str

    def create(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        tool_choice: dict | None = None,
        think: bool = False,
        throttle: bool = True,
    ) -> ChatResponse: ...


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def retry_pause(attempt: int, exc: ChatProviderError) -> float:
    if exc.retry_after is not None:
        return exc.retry_after + random.uniform(1, 3)
    base = min(60.0, 15.0 * (2 ** attempt))
    return base + random.uniform(0, base * 0.2)


def thinking_enabled() -> bool:
    """Whether extended/reasoning mode should be requested from the model.

    Controlled by the ENABLE_THINKING env var (default off — higher quality
    but materially slower). Applies to both providers: Anthropic's extended
    thinking and Ollama's `think` request field. Incompatible with a forced
    tool_choice, so callers that force a specific tool (e.g. ingest
    normalization) never pass this through.
    """
    return (os.environ.get("ENABLE_THINKING") or "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

class AnthropicChatProvider:

    def __init__(self, api_key: str | None = None):
        from anthropic import Anthropic
        from claude_client import (
            CHAT_MODEL,
            CHAT_TIMEOUT_SECONDS,
            CHAT_MAX_RETRIES,
            ANTHROPIC_SEMAPHORE,
        )

        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = Anthropic(
            api_key=key,
            timeout=CHAT_TIMEOUT_SECONDS,
            max_retries=CHAT_MAX_RETRIES,
        )
        # Shared with OCR (ingest.py) so the total number of concurrent
        # Anthropic calls across normalization, labeling, the agent, and OCR
        # never exceeds MAX_ANTHROPIC_CONCURRENT.
        self._semaphore = ANTHROPIC_SEMAPHORE
        self.explore_model = "claude-haiku-4-5-20251001"
        self.agent_model = CHAT_MODEL
        self.label_model = "claude-haiku-4-5-20251001"
        self.normalize_model = "claude-haiku-4-5-20251001"

    # -- cache breakpoints (Anthropic-specific) --

    @staticmethod
    def _add_cache_breakpoints(messages: list[dict]) -> list[dict]:
        if not messages:
            return messages
        msgs = list(messages)
        for i in range(len(msgs) - 1, -1, -1):
            msg = msgs[i]
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if content is None:
                break
            if isinstance(content, str):
                msgs[i] = {
                    **msg,
                    "content": [{
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }],
                }
            elif isinstance(content, list) and content:
                new_content = list(content)
                last = new_content[-1]
                if isinstance(last, dict):
                    last = {**last, "cache_control": {"type": "ephemeral"}}
                new_content[-1] = last
                msgs[i] = {**msg, "content": new_content}
            break
        return msgs

    # -- main call --

    def create(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        tool_choice: dict | None = None,
        think: bool = False,
        throttle: bool = True,
    ) -> ChatResponse:
        import anthropic

        t0 = time.time()
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            messages=self._add_cache_breakpoints(messages),
        )
        # The Anthropic API rejects an empty text block, so only attach
        # `system` when the caller actually has one (pipeline cluster-label
        # calls pass "").
        if system:
            kwargs["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if tools:
            kwargs["tools"] = [
                {**t, "cache_control": {"type": "ephemeral"}} for t in tools
            ]
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        # Extended thinking is incompatible with a forced tool_choice.
        if think and not (tool_choice and tool_choice.get("type") == "tool"):
            kwargs["thinking"] = {"type": "adaptive"}

        print(
            f"[chat.anthropic] calling {model}, max_tokens={max_tokens}, "
            f"messages={len(messages)}",
            flush=True,
        )

        # Background batch work (ingest normalization, cluster labeling)
        # shares a concurrency cap; interactive paths (the beat-book agent,
        # the research agent) opt out so they're never queued behind
        # background ingestion — see MAX_ANTHROPIC_CONCURRENT in claude_client.py.
        semaphore_cm = self._semaphore if throttle else contextlib.nullcontext()
        try:
            with semaphore_cm:
                with self._client.messages.stream(**kwargs) as stream:
                    for _event in stream:
                        pass
                    response = stream.get_final_message()
        except anthropic.RateLimitError as e:
            err = ChatRateLimitError(str(e))
            resp = getattr(e, "response", None)
            if resp is not None:
                ra = getattr(resp, "headers", {}).get("retry-after")
                if ra:
                    try:
                        err.retry_after = float(ra)
                    except (TypeError, ValueError):
                        pass
            raise err from e
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise ChatConnectionError(str(e)) from e
        except anthropic.APIStatusError as e:
            status = getattr(e, "status_code", None)
            if status and 500 <= status < 600:
                raise ChatServerError(str(e)) from e
            raise

        # Normalize ContentBlock objects → plain dicts
        content: list[dict] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                content.append({"type": "text", "text": block.text})
            elif btype == "tool_use":
                content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input or {},
                })

        usage: dict[str, Any] = {}
        u = getattr(response, "usage", None)
        if u:
            usage = {
                "input_tokens": getattr(u, "input_tokens", 0),
                "output_tokens": getattr(u, "output_tokens", 0),
                "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0),
                "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0),
            }

        elapsed = time.time() - t0
        print(
            f"[chat.anthropic] done in {elapsed:.1f}s, "
            f"stop_reason={response.stop_reason}, "
            f"blocks={[b.get('type') for b in content]}, "
            f"usage(in={usage.get('input_tokens','?')}, "
            f"out={usage.get('output_tokens','?')}, "
            f"cache_write={usage.get('cache_creation_input_tokens','?')}, "
            f"cache_read={usage.get('cache_read_input_tokens','?')})",
            flush=True,
        )

        return ChatResponse(content=content, stop_reason=response.stop_reason, usage=usage)


# ---------------------------------------------------------------------------
# Ollama (native /api/chat — works for cloud and local)
# ---------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_THINK_UNCLOSED_RE = re.compile(r"<think>.*", re.DOTALL)


def _strip_think_tags(text: str) -> str:
    """Remove Qwen-style <think>…</think> blocks from model output.

    Returns "" if the response was reasoning-only — falling back to the raw
    text would leak the <think> block instead of suppressing it.
    """
    stripped = _THINK_RE.sub("", text)
    stripped = _THINK_UNCLOSED_RE.sub("", stripped).strip()
    return stripped


class OllamaChatProvider:

    def __init__(
        self,
        host: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ):
        self._host = (
            host or os.environ.get("OLLAMA_CHAT_HOST", "https://ollama.com")
        ).rstrip("/")
        self._api_key = api_key or os.environ.get("OLLAMA_API_KEY", "")
        _model = model or os.environ.get("OLLAMA_CHAT_MODEL", "qwen3:8b")
        self.explore_model = _model
        self.agent_model = _model
        self.label_model = _model
        self.normalize_model = _model

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict]:
        out = []
        for t in tools:
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            })
        return out

    @staticmethod
    def _convert_messages(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})

        for msg in messages:
            role = msg["role"]
            content = msg.get("content", "")

            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue

            if not isinstance(content, list):
                out.append({"role": role, "content": str(content)})
                continue

            if role == "assistant":
                text_parts = []
                tool_calls = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "function": {
                                "name": block["name"],
                                "arguments": block.get("input", {}),
                            },
                        })
                omsg: dict[str, Any] = {
                    "role": "assistant",
                    "content": "\n".join(text_parts),
                }
                if tool_calls:
                    omsg["tool_calls"] = tool_calls
                out.append(omsg)

            elif role == "user":
                text_parts = []
                for block in content:
                    if isinstance(block, str):
                        text_parts.append(block)
                    elif block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif block.get("type") == "tool_result":
                        out.append({
                            "role": "tool",
                            "content": block.get("content", ""),
                        })
                if text_parts:
                    out.append({"role": "user", "content": "\n".join(text_parts)})
            else:
                out.append({"role": role, "content": str(content)})

        return out

    def create(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        tool_choice: dict | None = None,
        think: bool = False,
        throttle: bool = True,  # no-op: Ollama has no shared concurrency cap
    ) -> ChatResponse:
        import httpx

        actual_model = model or self.agent_model
        ollama_messages = self._convert_messages(system, messages)

        msg_chars = sum(len(m.get("content", "")) for m in ollama_messages)

        body: dict[str, Any] = {
            "model": actual_model,
            "messages": ollama_messages,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "num_ctx": 65536,
            },
            "think": think,
        }

        forced_tool_name: str | None = None

        if (
            tools
            and tool_choice
            and tool_choice.get("type") == "tool"
            and tool_choice.get("name")
        ):
            target = tool_choice["name"]
            schema = None
            for t in tools:
                if t.get("name") == target:
                    schema = t.get("input_schema", {})
                    break
            if schema:
                body["format"] = schema
                forced_tool_name = target
            else:
                body["tools"] = self._convert_tools(tools)
        elif tools and (not tool_choice or tool_choice.get("type") != "none"):
            body["tools"] = self._convert_tools(tools)

        t0 = time.time()
        print(
            f"[chat.ollama] calling {actual_model} at {self._host}, "
            f"max_tokens={max_tokens}, messages={len(messages)}, "
            f"msg_chars={msg_chars}",
            flush=True,
        )

        try:
            with httpx.Client(timeout=600.0) as client:
                resp = client.post(
                    f"{self._host}/api/chat",
                    headers=self._headers(),
                    json=body,
                )
        except httpx.ConnectError as e:
            raise ChatConnectionError(str(e)) from e
        except httpx.TimeoutException as e:
            raise ChatConnectionError(f"Timeout: {e}") from e

        if resp.status_code == 429:
            err = ChatRateLimitError(f"Ollama rate limit: {resp.text}")
            ra = resp.headers.get("retry-after")
            if ra:
                try:
                    err.retry_after = float(ra)
                except (TypeError, ValueError):
                    pass
            raise err
        if resp.status_code >= 500:
            raise ChatServerError(f"Ollama {resp.status_code}: {resp.text}")
        if resp.status_code >= 400:
            raise ChatProviderError(f"Ollama {resp.status_code}: {resp.text}")

        data = resp.json()
        message = data.get("message", {})

        content: list[dict] = []
        text = message.get("content", "")
        if text:
            text = _strip_think_tags(text)

        raw_tool_calls = message.get("tool_calls") or []

        if forced_tool_name and not raw_tool_calls and text:
            try:
                parsed = json.loads(text)
                content.append({
                    "type": "tool_use",
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "name": forced_tool_name,
                    "input": parsed,
                })
                stop_reason = "tool_use"
            except json.JSONDecodeError:
                content.append({"type": "text", "text": text})
                stop_reason = "end_turn"
        else:
            if text:
                content.append({"type": "text", "text": text})

            for tc in raw_tool_calls:
                func = tc.get("function", {})
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                content.append({
                    "type": "tool_use",
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "name": func.get("name", ""),
                    "input": args,
                })

            if raw_tool_calls:
                stop_reason = "tool_use"
            elif data.get("done_reason") == "length":
                stop_reason = "max_tokens"
            else:
                stop_reason = "end_turn"

        usage: dict[str, Any] = {}
        if "prompt_eval_count" in data:
            usage["input_tokens"] = data.get("prompt_eval_count", 0)
        if "eval_count" in data:
            usage["output_tokens"] = data.get("eval_count", 0)

        elapsed = time.time() - t0
        print(
            f"[chat.ollama] done in {elapsed:.1f}s, "
            f"stop_reason={stop_reason}, "
            f"blocks={[b.get('type') for b in content]}, "
            f"usage(in={usage.get('input_tokens','?')}, "
            f"out={usage.get('output_tokens','?')})",
            flush=True,
        )

        return ChatResponse(content=content, stop_reason=stop_reason, usage=usage)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_chat_provider(api_key: str | None = None) -> ChatProvider:
    provider = os.environ.get("CHAT_PROVIDER", "anthropic").strip().lower()
    if provider == "ollama":
        return OllamaChatProvider()
    return AnthropicChatProvider(api_key=api_key)
