"""
agent_tracer — log the token usage of agents you build yourself, so the token
tracker counts them next to Claude Code and Copilot.

Claude Code and Copilot write usage logs on their own. An agent you write
against the Anthropic API (your own loop, the Python SDK, LangChain, an
OpenAI-compatible client, ...) writes nothing, so there is nothing to read.
This module fixes that: every model call is appended, as one line, to

    ~/.claude/token-tracker/agents/<app>/<session>.jsonl

in the same shape Claude Code uses, and the tracker scans that folder on its
own. Only usage and names are written: model, token counts, tool names, agent
and loop ids. Never prompts, responses or tool inputs.

Copy this one file into your project; it needs nothing but the standard
library. Python 3.8+.

    from agent_tracer import TokenTracer

    tracer = TokenTracer(app="research-bot")
    client = tracer.instrument(anthropic.Anthropic())   # logs every create()

    with tracer.loop():                                  # one task / prompt
        reply = client.messages.create(...)              # logged as step 1
        ...                                              # tool calls, step 2...
        with tracer.subagent("summariser"):              # a delegated agent
            client.messages.create(...)                  # counted in the loop

Streaming, or a client it doesn't wrap: log the final message yourself with
`tracer.record(final_message)`. Any usage dict works too:
`tracer.record(usage={"input_tokens": 1200, "output_tokens": 300},
model="claude-opus-5-5")`.

Anthropic and OpenAI-style responses (chat completions and responses API) are
understood; costs are worked out by the tracker from the model name at live
list prices, like everything else.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import json
import os
import threading
import uuid
from pathlib import Path

__all__ = ["TokenTracer"]


def _default_root() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    home = Path(os.path.expanduser(env)) if env else Path.home() / ".claude"
    return home / "token-tracker" / "agents"


def _as_dict(obj) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
    return {k: getattr(obj, k) for k in ("id", "model", "usage", "content",
                                         "choices", "output")
            if hasattr(obj, k)}


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalise_usage(raw: dict) -> dict:
    """Any provider's usage -> Anthropic field names, which the tracker prices."""
    raw = _as_dict(raw)
    if "prompt_tokens" in raw or "input_tokens_details" in raw:
        # OpenAI: cached tokens are inside the prompt count, not beside it.
        prompt = _int(raw.get("prompt_tokens", raw.get("input_tokens")))
        details = _as_dict(raw.get("prompt_tokens_details")
                           or raw.get("input_tokens_details"))
        cached = _int(details.get("cached_tokens"))
        out_details = _as_dict(raw.get("completion_tokens_details")
                               or raw.get("output_tokens_details"))
        return {
            "input_tokens": max(0, prompt - cached),
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0,
            "output_tokens": _int(raw.get("completion_tokens",
                                          raw.get("output_tokens"))),
            "output_tokens_details": {
                "thinking_tokens": _int(out_details.get("reasoning_tokens"))},
        }
    usage = {k: raw[k] for k in (
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "service_tier", "inference_geo", "speed",
    ) if raw.get(k) is not None}
    for key in ("cache_creation", "server_tool_use", "output_tokens_details"):
        if raw.get(key) is not None:
            usage[key] = _as_dict(raw[key])
    usage.setdefault("input_tokens", 0)
    usage.setdefault("output_tokens", 0)
    return usage


def _tool_names(resp: dict) -> list[str]:
    names = []
    for block in resp.get("content") or []:            # Anthropic
        block = _as_dict(block)
        if block.get("type") in ("tool_use", "server_tool_use") and block.get("name"):
            names.append(str(block["name"]))
    for choice in resp.get("choices") or []:           # OpenAI chat
        message = _as_dict(_as_dict(choice).get("message"))
        for call in message.get("tool_calls") or []:
            fn = _as_dict(_as_dict(call).get("function"))
            if fn.get("name"):
                names.append(str(fn["name"]))
    for item in resp.get("output") or []:              # OpenAI responses
        item = _as_dict(item)
        if item.get("type") == "function_call" and item.get("name"):
            names.append(str(item["name"]))
    return names


class TokenTracer:
    """Appends one usage line per model call. Thread-safe; loops and
    subagents are tracked per thread, so parallel workers don't mix."""

    def __init__(self, app: str, session: str | None = None,
                 project: str | None = None, root: str | Path | None = None):
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in app) or "agent"
        self.app = app
        self.session = session or str(uuid.uuid4())
        self.project = project or os.getcwd()
        self.path = Path(root) if root else _default_root()
        self.path = self.path / safe / f"{self.session}.jsonl"
        self._lock = threading.Lock()
        self._local = threading.local()

    # -- context ----------------------------------------------------------- #

    def _stack(self) -> list[dict]:
        if not hasattr(self._local, "stack"):
            self._local.stack = []
        return self._local.stack

    @contextlib.contextmanager
    def loop(self, loop_id: str | None = None):
        """One task: a prompt and every model call it takes to finish it."""
        frame = {"loop": loop_id or str(uuid.uuid4()), "agent": "main",
                 "agent_id": "", "depth": 0, "sidechain": False}
        self._stack().append(frame)
        try:
            yield frame["loop"]
        finally:
            self._stack().pop()

    @contextlib.contextmanager
    def subagent(self, agent_type: str, agent_id: str | None = None):
        """Work delegated to another agent, counted in the current loop."""
        parent = self._stack()[-1] if self._stack() else None
        frame = {"loop": parent["loop"] if parent else str(uuid.uuid4()),
                 "agent": agent_type, "agent_id": agent_id or uuid.uuid4().hex[:17],
                 "depth": (parent["depth"] if parent else 0) + 1, "sidechain": True}
        self._stack().append(frame)
        try:
            yield frame["agent_id"]
        finally:
            self._stack().pop()

    # -- logging ----------------------------------------------------------- #

    def record(self, response=None, *, usage: dict | None = None,
               model: str | None = None, tools: list[str] | None = None,
               request_id: str | None = None) -> None:
        """Log one model call from its response, or from a usage dict."""
        resp = _as_dict(response)
        raw = usage if usage is not None else resp.get("usage")
        if raw is None:
            return
        frame = self._stack()[-1] if self._stack() else {
            "loop": "", "agent": "main", "agent_id": "", "depth": 0,
            "sidechain": False}
        names = tools if tools is not None else _tool_names(resp)
        row = {
            "type": "assistant",
            "uuid": str(uuid.uuid4()),
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()
                         .replace("+00:00", "Z"),
            "sessionId": self.session,
            "requestId": request_id or resp.get("id") or str(uuid.uuid4()),
            "entrypoint": "custom-agent",
            "appName": self.app,
            "cwd": self.project,
            "promptId": frame["loop"] or None,
            "isSidechain": frame["sidechain"],
            "agentId": frame["agent_id"] or None,
            "attributionAgent": frame["agent"],
            "spawnDepth": frame["depth"],
            "message": {
                "model": model or resp.get("model") or "unknown",
                "usage": _normalise_usage(raw),
                "content": [{"type": "tool_use", "name": n} for n in names],
            },
        }
        line = json.dumps(row, separators=(",", ":")) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)

    def instrument(self, client):
        """Wrap a client so every non-streaming call is logged. Works on the
        Anthropic SDK (`client.messages.create`) and OpenAI-style clients
        (`client.chat.completions.create`, `client.responses.create`)."""
        targets = [("messages", "create"), ("chat.completions", "create"),
                   ("responses", "create")]
        for owner_path, name in targets:
            owner = client
            try:
                for part in owner_path.split("."):
                    owner = getattr(owner, part)
                original = getattr(owner, name)
            except AttributeError:
                continue
            if getattr(original, "_token_traced", False):
                continue

            @functools.wraps(original)
            def wrapped(*args, __original=original, **kwargs):
                result = __original(*args, **kwargs)
                if not kwargs.get("stream"):
                    try:
                        self.record(result)
                    except Exception:
                        pass                # never break the caller's agent
                return result

            wrapped._token_traced = True
            try:
                setattr(owner, name, wrapped)
            except (AttributeError, TypeError):
                continue
        return client
