"""
GitHub Copilot Chat usage, read from VS Code's local chat logs.

Copilot Chat in VS Code (and VS Code Insiders) keeps one log per chat session
under the editor's user-data folder:

    <user data>/User/workspaceStorage/<id>/chatSessions/<session>.jsonl
    <user data>/User/globalStorage/emptyWindowChatSessions/<session>.jsonl

Each line is an edit to the session object: kind 0 is a full snapshot, kind 1
sets the value at path `k`, and kind 2 appends to the list at `k` (cutting it
to index `i` first, when given). Replaying the lines gives the session, whose
`requests` record, for each request:

    modelId                   what was picked, e.g. copilot/claude-sonnet-4.5
                              or copilot/auto
    result.metadata.resolvedModel   the model that actually answered
    promptTokens              the prompt of the request's final model call
    completionTokens          output across all of the request's model calls
    result.metadata.toolCallRounds[].thinking.tokens   thinking per call
    copilotCredits            what Copilot charged, in GitHub AI credits
    modeInfo / agent          agent, ask or edit mode
    subAgentInvocationId      set when a subagent ran the request

Only these metadata fields are read: never the prompt or the response text.
Copilot does not record each model call's prompt or any cache figures, so the
token counts here undercount what an agent run actually sent, and no dollar
figure can be worked out from them. What Copilot charged is recorded, though,
in AI credits; GitHub publishes their dollar value (1 AI credit = $0.01 USD),
so a Copilot turn's cost is its credits times that rate.

Not covered: Copilot in JetBrains, Visual Studio, Xcode and the Copilot CLI
keep no token counts on disk.

Standard library only. Python 3.8+.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from token_report import Pricing, Turn

# Folder name under the platform's app-data directory -> how it is shown.
EDITORS = {
    "Code": "VS Code",
    "Code - Insiders": "VS Code Insiders",
    "VSCodium": "VSCodium",
}

MODE_LABELS = {"agent": "agent", "ask": "ask", "edit": "edit"}


def _app_data_dirs() -> list[Path]:
    home = Path(os.path.expanduser("~"))
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support"]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata)] if appdata else []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return [Path(xdg) if xdg else home / ".config"]


def session_files() -> list[tuple[Path, str]]:
    """Every Copilot chat log on this machine, with the editor it belongs to."""
    found: list[tuple[Path, str]] = []
    for base in _app_data_dirs():
        for folder, editor in EDITORS.items():
            user = base / folder / "User"
            if not user.is_dir():
                continue
            patterns = [
                ("workspaceStorage", "*/chatSessions/*.json*"),
                ("globalStorage", "emptyWindowChatSessions/*.json*"),
            ]
            for sub, pattern in patterns:
                try:
                    found.extend((p, editor) for p in (user / sub).glob(pattern)
                                 if p.suffix in (".json", ".jsonl"))
                except OSError:
                    continue
    return found


def replay(path: Path) -> dict | None:
    """Rebuild a session object from its log (or read an older .json file)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            if path.suffix == ".json":
                state = json.load(fh)
                return state if isinstance(state, dict) else None
            state = None
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    op = json.loads(line)
                except ValueError:
                    continue                      # a line still being written
                kind = op.get("kind")
                if kind == 0:
                    state = op.get("v")
                    continue
                if not isinstance(state, dict):
                    continue
                keys = op.get("k") or []
                try:
                    target = state
                    for key in keys[:-1]:
                        target = target[key]
                    if kind == 1:
                        if keys:
                            target[keys[-1]] = op.get("v")
                        elif isinstance(op.get("v"), dict):
                            state = op["v"]
                    elif kind == 2:
                        items = target[keys[-1]] if keys else target
                        if "i" in op:
                            del items[op["i"]:]
                        items.extend(op.get("v") or [])
                except (KeyError, IndexError, TypeError, AttributeError):
                    continue                      # an edit to a part never seen
            return state if isinstance(state, dict) else None
    except (OSError, ValueError):
        return None


def _workspace(path: Path) -> str:
    """The folder (or .code-workspace file) the chat ran in, as a path."""
    meta = path.parent.parent / "workspace.json"
    try:
        with open(meta, "r", encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return ""
    uri = info.get("folder") or info.get("workspace") or ""
    return unquote(urlparse(uri).path) if uri else ""


def _project(path: Path) -> str:
    """The workspace folder's name, from VS Code's workspace.json."""
    name = Path(_workspace(path)).name
    if name.endswith(".code-workspace"):
        name = name[: -len(".code-workspace")]
    return name or "(no folder)"


def _mode(request: dict) -> str:
    info = request.get("modeInfo") or {}
    mode = (info.get("telemetryModeId") or info.get("modeId")
            or info.get("kind") or "")
    if info.get("isBuiltin") is False:
        return "custom agent"
    if mode:
        return MODE_LABELS.get(mode, mode)
    agent = ((request.get("agent") or {}).get("id") or "").lower()
    if "edits" in agent:
        return "agent"
    return "ask" if agent else ""


def _model(request: dict) -> str:
    """The model that answered: Copilot's "Auto" is resolved where recorded."""
    meta = ((request.get("result") or {}).get("metadata") or {})
    resolved = meta.get("resolvedModel")
    if resolved:
        return str(resolved)
    for call in meta.get("toolCallRounds") or []:
        if isinstance(call, dict) and call.get("modelId"):
            return str(call["modelId"])
    picked = str(request.get("modelId") or "")
    return picked.split("/", 1)[1] if picked.startswith("copilot/") else picked


def _stamp(millis, use_utc: bool) -> dt.datetime | None:
    try:
        stamp = dt.datetime.fromtimestamp(float(millis) / 1000.0, dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return stamp if use_utc else stamp.astimezone()


def model_label(pricing: Pricing, model_id: str) -> str:
    """ "Copilot · Sonnet 4.5", so Copilot's use of a model stays apart from
    Claude Code's use of the same one."""
    return f"Copilot · {pricing.label(model_id) if model_id else 'unknown'}"


def turns_from_file(path: Path, editor: str, pricing: Pricing,
                    use_utc: bool = False,
                    include_sidechains: bool = True) -> list[Turn]:
    """One Turn per completed Copilot request in this chat log."""
    state = replay(path)
    if not state:
        return []
    session = str(state.get("sessionId") or path.stem)
    project = _project(path)
    folder = _workspace(path)
    title = str(state.get("customTitle") or "")[:200]
    location = str(state.get("initialLocation") or "")
    turns: list[Turn] = []
    for request in state.get("requests") or []:
        if not isinstance(request, dict):
            continue
        prompt = request.get("promptTokens")
        output = request.get("completionTokens")
        if prompt is None and output is None:
            continue                              # cancelled or still running
        is_sub = bool(request.get("subAgentInvocationId"))
        if is_sub and not include_sidechains:
            continue
        meta = ((request.get("result") or {}).get("metadata") or {})
        thinking = 0
        rounds = [c for c in meta.get("toolCallRounds") or [] if isinstance(c, dict)]
        tools: list[str] = []
        for call in rounds:
            thinking += int(((call.get("thinking") or {}).get("tokens")) or 0)
            tools.extend(str(t.get("name")) for t in call.get("toolCalls") or []
                         if isinstance(t, dict) and t.get("name"))
        credits = request.get("copilotCredits")
        try:
            credits = None if credits is None else float(credits)
        except (TypeError, ValueError):
            credits = None
        rate = pricing.copilot_usd_per_credit
        cost = credits * rate if credits is not None and rate is not None else None
        model = _model(request)
        stamp = _stamp(request.get("timestamp"), use_utc)
        tok_in, tok_out = int(prompt or 0), int(output or 0)
        mode = _mode(request)
        parts: dict[str, float] = {}
        for part in request.get("promptTokenDetails") or []:
            try:
                share = float(part.get("percentageOfPrompt")) / 100.0
            except (TypeError, ValueError, AttributeError):
                continue
            label = str(part.get("label") or part.get("category") or "other")
            parts[label] = parts.get(label, 0.0) + share * tok_in
        turns.append(Turn(
            ts=stamp,
            day=stamp.date().isoformat() if stamp else "(undated)",
            model=model,
            model_label=model_label(pricing, model),
            session=session,
            surface=f"Copilot · {editor}",
            project=project,
            git_branch="",
            is_sidechain=is_sub,
            skill="", plugin="", mcp="", effort="",
            cli_version=str(state.get("version") or ""),
            input=tok_in, output=tok_out, thinking=thinking,
            cache_read=0, cache_write_5m=0, cache_write_1h=0,
            web_searches=0, web_fetches=0,
            cost=cost or 0.0, priced=cost is not None,
            raw_total=tok_in + tok_out,
            via="Copilot",
            credits=credits,
            mode=" · ".join(x for x in (mode and f"{mode} mode",
                                        location and f"{location}") if x),
            request_id="copilot:" + str(request.get("requestId") or ""),
            # One request is one agent loop: the prompt you sent and every
            # model call ("round") Copilot made answering it.
            agent=("subagent" if is_sub else (mode or "chat")),
            agent_id=str(request.get("subAgentInvocationId") or ""),
            loop_id="copilot:" + str(request.get("requestId") or ""),
            spawn_depth=1 if is_sub else 0,
            tools=tools,
            steps=max(1, len(rounds)),
            prompt_parts=parts or None,
            cwd=folder,
            title=title,
            named=False,
        ))
    return turns


def collect(pricing: Pricing, use_utc: bool = False,
            include_sidechains: bool = True) -> tuple[list[Turn], int]:
    """(turns, files read) across every Copilot chat log on this machine."""
    turns: list[Turn] = []
    files = session_files()
    for path, editor in files:
        turns.extend(turns_from_file(path, editor, pricing, use_utc,
                                     include_sidechains))
    return turns, len(files)
