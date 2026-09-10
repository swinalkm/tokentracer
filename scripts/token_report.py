#!/usr/bin/env python3
"""
claude-token-tracker — account-local token accounting for Claude.

Scans the JSONL transcripts that Claude Code (terminal), the VS Code / JetBrains
extension, and Cowork write to ~/.claude/projects/, then reports exactly how many
tokens each turn consumed and what it cost.

Coverage note: claude.ai web, the desktop chat UI, and the mobile apps do NOT write
transcripts to disk and expose no per-conversation usage API, so they cannot be
included. See Settings -> Usage for those. This tool never guesses at them.

Standard library only. Python 3.8+.

Usage:
    python3 token_report.py                      # last 30 days -> HTML + summary
    python3 token_report.py --days 7 --open
    python3 token_report.py --today
    python3 token_report.py --json
    python3 token_report.py --session <id>       # turn-by-turn for one session
    python3 token_report.py --group-by skill

Licence: MIT
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import webbrowser
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PRICING = SCRIPT_DIR / "pricing.json"

# Surfaces, keyed by the transcript's `entrypoint` field.
SURFACE_LABELS = {
    "cli": "Terminal (Claude Code)",
    "claude-code": "Terminal (Claude Code)",
    "local-agent": "Cowork (desktop)",
    "vscode": "VS Code extension",
    "vscode-extension": "VS Code extension",
    "jetbrains": "JetBrains extension",
    "intellij": "JetBrains extension",
    "desktop": "Claude Code (desktop app)",
    "web": "Claude Code on the web",
    "sdk": "Agent SDK",
    "sdk-py": "Agent SDK (Python)",
    "sdk-ts": "Agent SDK (TypeScript)",
    "github-action": "GitHub Action",
    "slack": "Claude in Slack",
    "mcp": "MCP client",
    "scheduled-task": "Scheduled task",
}


# --------------------------------------------------------------------------- #
# pricing
# --------------------------------------------------------------------------- #

class Pricing:
    """Per-million-token rates, loaded from pricing.json."""

    def __init__(self, path: Path):
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self.models = raw.get("models", {})
        self.modifiers = raw.get("modifiers", {})
        self.web_search_per_1k = float(raw.get("web_search_per_1k_requests", 0.0))
        self.checked = raw.get("_checked", "unknown")
        self.source = raw.get("_source", "")
        self.unknown_models: set[str] = set()

    @staticmethod
    def _normalise(model_id: str) -> str:
        """claude-haiku-4-5-20251001 -> claude-haiku-4-5 (strip date suffix)."""
        parts = (model_id or "").split("-")
        while parts and parts[-1].isdigit() and len(parts[-1]) == 8:
            parts.pop()
        return "-".join(parts)

    def rates(self, model_id: str, speed: str = "standard") -> dict | None:
        key = self._normalise(model_id)
        base = self.models.get(key)
        if base is None:
            # Longest-prefix fallback, so an unseen dated variant still prices.
            candidates = [k for k in self.models if key.startswith(k)]
            if candidates:
                base = self.models[max(candidates, key=len)]
            else:
                if model_id:
                    self.unknown_models.add(model_id)
                return None
        if speed == "fast":
            fast = (self.modifiers.get("fast_mode") or {}).get(key)
            if fast:
                merged = dict(base)
                merged.update(fast)
                return merged
        return base

    def label(self, model_id: str) -> str:
        key = self._normalise(model_id)
        entry = self.models.get(key)
        if entry:
            return entry.get("label", key)
        candidates = [k for k in self.models if key.startswith(k)]
        if candidates:
            return self.models[max(candidates, key=len)].get("label", key)
        return model_id or "unknown"


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

class Turn:
    """One priced assistant turn (one API request)."""

    __slots__ = (
        "ts", "day", "model", "model_label", "session", "surface", "project",
        "git_branch", "is_sidechain", "skill", "plugin", "mcp", "effort",
        "input", "output", "thinking", "cache_read", "cache_write_5m",
        "cache_write_1h", "web_searches", "web_fetches", "cost", "priced",
        "raw_total", "cli_version",
    )

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))

    @property
    def billable_input(self) -> int:
        return (self.input or 0) + (self.cache_read or 0) + \
               (self.cache_write_5m or 0) + (self.cache_write_1h or 0)


def _parse_ts(value: str, use_utc: bool) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        stamp = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp if use_utc else stamp.astimezone()


def _project_name(cwd: str | None) -> str:
    if not cwd:
        return "(unknown)"
    if "local-agent-mode-sessions" in cwd:
        return "Cowork session"
    return Path(cwd).name or cwd


def discover_roots(explicit: list[str] | None) -> list[Path]:
    if explicit:
        return [Path(os.path.expanduser(p)) for p in explicit]

    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        # A configured directory is a declaration, not a hint. Honour it on
        # its own: also scanning the default would silently blend a second
        # installation's transcripts into the totals, and would hide the fact
        # that the configured directory holds nothing.
        root = Path(os.path.expanduser(env)) / "projects"
        return [root] if root.is_dir() else []

    home = Path(os.path.expanduser("~"))
    candidates = [
        home / ".claude" / "projects",
        home / ".config" / "claude" / "projects",
    ]
    return [p for p in candidates if p.is_dir()]


def _surface_label(entrypoint: str | None) -> str:
    """Human name for a transcript `entrypoint`.

    New surfaces keep arriving and several now ship a `claude-` prefix
    (`claude-vscode`), so fall back to the unprefixed key before giving up and
    showing the raw value.
    """
    key = (entrypoint or "").strip()
    if not key:
        return "(unrecorded)"
    if key in SURFACE_LABELS:
        return SURFACE_LABELS[key]
    stripped = key[len("claude-"):] if key.startswith("claude-") else key
    return SURFACE_LABELS.get(stripped, key)


def row_usage(row: object) -> dict | None:
    """Return the API `usage` block for a billable assistant row, else None."""
    if not isinstance(row, dict):
        return None
    message = row.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    return usage if isinstance(usage, dict) else None


def row_dedupe_key(row: dict) -> str | None:
    """The identity of the underlying API request.

    One request can be logged more than once (re-render, resume, sidechain
    echo). Callers charge each key exactly once.
    """
    message = row.get("message") or {}
    return row.get("requestId") or message.get("id") or row.get("uuid")


def turn_from_row(row: dict, usage: dict, pricing: Pricing,
                  use_utc: bool = False, fallback_session: str = "") -> Turn:
    """Price one transcript row into a Turn. Assumes `row_usage` said yes."""
    message = row["message"]
    stamp = _parse_ts(row.get("timestamp", ""), use_utc)

    cache_detail = usage.get("cache_creation")
    if isinstance(cache_detail, dict):
        write_5m = int(cache_detail.get("ephemeral_5m_input_tokens") or 0)
        write_1h = int(cache_detail.get("ephemeral_1h_input_tokens") or 0)
    else:
        # Older transcripts only carry the total. Assume the cheaper
        # 5-minute rate rather than overstating the bill.
        write_5m = int(usage.get("cache_creation_input_tokens") or 0)
        write_1h = 0

    details = usage.get("output_tokens_details")
    thinking = 0
    if isinstance(details, dict):
        thinking = int(details.get("thinking_tokens") or 0)

    server = usage.get("server_tool_use")
    searches = fetches = 0
    if isinstance(server, dict):
        searches = int(server.get("web_search_requests") or 0)
        fetches = int(server.get("web_fetch_requests") or 0)

    model_id = message.get("model") or ""
    speed = usage.get("speed") or "standard"
    rates = pricing.rates(model_id, speed)

    tok_in = int(usage.get("input_tokens") or 0)
    tok_out = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)

    if rates:
        cost = (
            tok_in / 1e6 * rates["input"]
            + cache_read / 1e6 * rates["cache_read"]
            + write_5m / 1e6 * rates["cache_write_5m"]
            + write_1h / 1e6 * rates["cache_write_1h"]
            + tok_out / 1e6 * rates["output"]
        )
        if usage.get("service_tier") == "batch":
            cost *= float(pricing.modifiers.get("batch_multiplier", 1.0))
        if usage.get("inference_geo") == "us":
            cost *= float(
                pricing.modifiers.get("inference_geo_us_multiplier", 1.0)
            )
        cost += searches / 1000.0 * pricing.web_search_per_1k
        priced = True
    else:
        cost, priced = 0.0, False

    return Turn(
        ts=stamp,
        day=stamp.date().isoformat() if stamp else "(undated)",
        model=model_id,
        model_label=pricing.label(model_id),
        session=row.get("sessionId") or fallback_session,
        surface=_surface_label(row.get("entrypoint")),
        project=_project_name(row.get("cwd")),
        git_branch=row.get("gitBranch") or "",
        is_sidechain=bool(row.get("isSidechain")),
        skill=row.get("attributionSkill") or "",
        plugin=row.get("attributionPlugin") or "",
        mcp=row.get("attributionMcpServer") or "",
        effort=row.get("effort") or "",
        cli_version=row.get("version") or "",
        input=tok_in,
        output=tok_out,
        thinking=thinking,
        cache_read=cache_read,
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        web_searches=searches,
        web_fetches=fetches,
        cost=cost,
        priced=priced,
        raw_total=tok_in + tok_out + cache_read + write_5m + write_1h,
    )


def collect_turns(roots: list[Path], pricing: Pricing, use_utc: bool,
                  include_sidechains: bool = True) -> tuple[list[Turn], dict]:
    """Read every transcript under `roots` and return priced turns + stats."""
    seen_requests: set[str] = set()
    turns: list[Turn] = []
    stats = {"files": 0, "lines": 0, "bad_lines": 0, "duplicates": 0}

    files: list[Path] = []
    for root in roots:
        files.extend(sorted(root.rglob("*.jsonl")))

    for path in files:
        stats["files"] += 1
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                stats["lines"] += 1
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    stats["bad_lines"] += 1
                    continue
                if not isinstance(row, dict):
                    stats["bad_lines"] += 1
                    continue

                usage = row_usage(row)
                if usage is None:
                    continue

                dedupe_key = row_dedupe_key(row)
                if dedupe_key:
                    if dedupe_key in seen_requests:
                        stats["duplicates"] += 1
                        continue
                    seen_requests.add(dedupe_key)

                if row.get("isSidechain") and not include_sidechains:
                    continue

                turns.append(turn_from_row(
                    row, usage, pricing, use_utc, fallback_session=path.stem
                ))

    return turns, stats


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #

BUCKET_FIELDS = {
    "day": "day",
    "model": "model_label",
    "surface": "surface",
    "session": "session",
    "project": "project",
    "skill": "skill",
    "plugin": "plugin",
    "mcp": "mcp",
}


def blank_bucket() -> dict:
    return {
        "turns": 0, "cost": 0.0, "input": 0, "output": 0, "thinking": 0,
        "cache_read": 0, "cache_write": 0, "raw_total": 0, "searches": 0,
        "unpriced": 0, "sessions": set(),
    }


def aggregate(turns: list[Turn], field: str) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(blank_bucket)
    for turn in turns:
        key = getattr(turn, field) or "(none)"
        bucket = out[key]
        bucket["turns"] += 1
        bucket["cost"] += turn.cost or 0.0
        bucket["input"] += turn.input or 0
        bucket["output"] += turn.output or 0
        bucket["thinking"] += turn.thinking or 0
        bucket["cache_read"] += turn.cache_read or 0
        bucket["cache_write"] += (turn.cache_write_5m or 0) + (turn.cache_write_1h or 0)
        bucket["raw_total"] += turn.raw_total or 0
        bucket["searches"] += turn.web_searches or 0
        bucket["sessions"].add(turn.session)
        if not turn.priced:
            bucket["unpriced"] += 1
    return dict(out)


def totals(turns: list[Turn]) -> dict:
    agg = blank_bucket()
    for turn in turns:
        agg["turns"] += 1
        agg["cost"] += turn.cost or 0.0
        agg["input"] += turn.input or 0
        agg["output"] += turn.output or 0
        agg["thinking"] += turn.thinking or 0
        agg["cache_read"] += turn.cache_read or 0
        agg["cache_write"] += (turn.cache_write_5m or 0) + (turn.cache_write_1h or 0)
        agg["raw_total"] += turn.raw_total or 0
        agg["searches"] += turn.web_searches or 0
        agg["sessions"].add(turn.session)
        if not turn.priced:
            agg["unpriced"] += 1
    return agg


def day_span(day_keys: list[str]) -> list[str]:
    """Every calendar day from first to last, so idle days show as gaps not absences."""
    real = sorted(k for k in day_keys if k != "(undated)")
    if not real:
        return []
    first = dt.date.fromisoformat(real[0])
    last = dt.date.fromisoformat(real[-1])
    if (last - first).days > 400:          # guard against a stray old timestamp
        return real
    out, cursor = [], first
    while cursor <= last:
        out.append(cursor.isoformat())
        cursor += dt.timedelta(days=1)
    return out


def cache_savings(turns: list[Turn], pricing: Pricing) -> float:
    """What the cache reads would have cost at full input price, minus what they did."""
    saved = 0.0
    for turn in turns:
        rates = pricing.rates(turn.model, "standard")
        if not rates or not turn.cache_read:
            continue
        saved += turn.cache_read / 1e6 * (rates["input"] - rates["cache_read"])
    return saved


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #

def fmt_int(value: float | int) -> str:
    return f"{int(round(value)):,}"


def fmt_tokens(value: float | int) -> str:
    value = float(value)
    if value >= 1e9:
        return f"{value / 1e9:.2f}B"
    if value >= 1e6:
        return f"{value / 1e6:.2f}M"
    if value >= 1e3:
        return f"{value / 1e3:.1f}k"
    return str(int(value))


def fmt_money(value: float) -> str:
    if value and abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


# --------------------------------------------------------------------------- #
# HTML dashboard
# --------------------------------------------------------------------------- #

def esc(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def svg_bars(series: list[tuple[str, float]], value_fmt, height: int = 190) -> str:
    """Vertical bar chart, no dependencies. series = [(label, value), ...]"""
    if not series:
        return '<p class="empty">No data in this range.</p>'

    peak = max((v for _, v in series), default=0.0) or 1.0
    count = len(series)
    slot = max(100.0 / count, 0.0001)
    bar_w = min(slot * 0.62, 7.0)
    show_every = max(1, count // 12)

    bars = []
    for index, (label, value) in enumerate(series):
        pct = max((value / peak) * 100.0, 0.6 if value > 0 else 0.0)
        left = slot * index + (slot - bar_w) / 2
        tip = f"{label}: {value_fmt(value)}"
        bars.append(
            f'<div class="bar-slot" style="left:{left:.4f}%;width:{bar_w:.4f}%" '
            f'title="{esc(tip)}">'
            f'<div class="bar" style="height:{pct:.3f}%"></div></div>'
        )
        if index % show_every == 0 or index == count - 1:
            bars.append(
                f'<div class="bar-label" style="left:{slot * index:.4f}%;'
                f'width:{slot:.4f}%">{esc(label[-5:])}</div>'
            )

    return (
        f'<div class="chart" style="height:{height}px">'
        f'<div class="chart-peak">{esc(value_fmt(peak))}</div>'
        f'<div class="plot">{"".join(bars)}</div>'
        f"</div>"
    )


def table(headers: list[str], rows: list[list[str]], numeric_from: int = 1) -> str:
    if not rows:
        return '<p class="empty">Nothing recorded.</p>'
    head = "".join(
        f'<th class="{"num" if i >= numeric_from else ""}">{esc(h)}</th>'
        for i, h in enumerate(headers)
    )
    body = []
    for row in rows:
        cells = "".join(
            f'<td class="{"num" if i >= numeric_from else ""}">{cell}</td>'
            for i, cell in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    return (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def bucket_rows(buckets: dict[str, dict], sort_key="cost", limit: int | None = None,
                sort_alpha: bool = False) -> list[list[str]]:
    items = list(buckets.items())
    if sort_alpha:
        items.sort(key=lambda kv: kv[0])
    else:
        items.sort(key=lambda kv: kv[1][sort_key], reverse=True)
    if limit:
        items = items[:limit]
    rows = []
    for name, data in items:
        rows.append([
            esc(name),
            fmt_int(data["turns"]),
            fmt_tokens(data["raw_total"]),
            fmt_tokens(data["output"]),
            fmt_money(data["cost"]),
        ])
    return rows


CSS = """
:root{color-scheme:light dark;
--bg:#faf9f7;--card:#ffffff;--ink:#141413;--ink2:#5f5e5a;--ink3:#8a8880;
--line:#e5e3dc;--accent:#2a78d6;--accent2:#b5d4f4;--warn:#854f0b;--warnbg:#faeeda}
@media (prefers-color-scheme:dark){:root{
--bg:#141413;--card:#1e1e1c;--ink:#f0efec;--ink2:#b8b6ae;--ink3:#8a8880;
--line:#33322e;--accent:#3987e5;--accent2:#1b4a80;--warn:#fac775;--warnbg:#2e2313}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1060px;margin:0 auto;padding:40px 24px 72px}
h1{font-size:24px;font-weight:600;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:16px;font-weight:600;margin:40px 0 12px}
.sub{color:var(--ink2);font-size:13px;margin:0}
.note{background:var(--warnbg);color:var(--warn);border-radius:10px;
padding:12px 16px;font-size:13px;line-height:1.55;margin:22px 0 0}
.note b{font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:12px;margin:24px 0 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.tile .k{font-size:12px;color:var(--ink2);margin:0 0 6px;text-transform:none}
.tile .v{font-size:23px;font-weight:600;margin:0;letter-spacing:-.02em;
font-variant-numeric:tabular-nums}
.tile .x{font-size:12px;color:var(--ink3);margin:4px 0 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px 18px 12px;margin-top:12px}
.chart{position:relative;padding:14px 0 26px}
.chart-peak{position:absolute;top:0;left:0;font-size:11px;color:var(--ink3);
font-variant-numeric:tabular-nums}
.plot{position:relative;height:100%;border-bottom:1px solid var(--line)}
.bar-slot{position:absolute;bottom:0;top:0;display:flex;align-items:flex-end}
.bar{width:100%;background:var(--accent);border-radius:3px 3px 0 0;min-height:1px;
transition:opacity .12s}
.bar-slot:hover .bar{opacity:.62}
.bar-label{position:absolute;bottom:-24px;text-align:center;font-size:10px;
color:var(--ink3);font-variant-numeric:tabular-nums;overflow:hidden;white-space:nowrap}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);
white-space:nowrap}
th{font-size:11.5px;font-weight:600;color:var(--ink2);text-transform:uppercase;
letter-spacing:.04em}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:color-mix(in srgb,var(--accent) 6%,transparent)}
.empty{color:var(--ink3);font-size:13px;margin:4px 0 12px}
footer{margin-top:48px;padding-top:18px;border-top:1px solid var(--line);
color:var(--ink3);font-size:12px;line-height:1.7}
footer a{color:var(--accent)}
code{background:color-mix(in srgb,var(--ink) 8%,transparent);padding:1px 5px;
border-radius:4px;font-size:12.5px}
"""


def render_html(turns: list[Turn], stats: dict, pricing: Pricing,
                window_label: str, roots: list[Path]) -> str:
    grand = totals(turns)
    saved = cache_savings(turns, pricing)
    per_day = aggregate(turns, "day")
    days_sorted = sorted(k for k in per_day if k != "(undated)")
    calendar = day_span(days_sorted)

    def day_value(key: str, field: str) -> float:
        return float(per_day.get(key, {}).get(field, 0.0))

    daily_cost = [(d, day_value(d, "cost")) for d in calendar]
    busiest = max(per_day.items(), key=lambda kv: kv[1]["cost"], default=(None, None))
    active_days = len(days_sorted) or 1
    sidechain_turns = sum(1 for t in turns if t.is_sidechain)

    generated = dt.datetime.now().strftime("%d %b %Y, %H:%M")

    tiles = [
        ("Estimated cost", fmt_money(grand["cost"]),
         f"{window_label} · {fmt_money(grand['cost'] / active_days)}/active day"),
        ("Tokens billed", fmt_tokens(grand["raw_total"]),
         f"{fmt_tokens(grand['output'])} written by Claude"),
        ("Turns", fmt_int(grand["turns"]),
         f"{fmt_int(len(grand['sessions']))} sessions"
         + (f" · {fmt_int(sidechain_turns)} subagent" if sidechain_turns else "")),
        ("Saved by caching", fmt_money(saved),
         "vs. re-reading at full price"),
    ]
    if grand["searches"]:
        tiles.append((
            "Web searches", fmt_int(grand["searches"]),
            fmt_money(grand["searches"] / 1000 * pricing.web_search_per_1k),
        ))

    tile_html = "".join(
        f'<div class="tile"><p class="k">{esc(k)}</p><p class="v">{esc(v)}</p>'
        f'<p class="x">{esc(x)}</p></div>'
        for k, v, x in tiles
    )

    sections = []

    sections.append(
        f'<h2>Cost per day</h2><div class="card">'
        f"{svg_bars(daily_cost, fmt_money)}</div>"
    )

    sections.append(
        f'<h2>Tokens per day</h2><div class="card">'
        f"{svg_bars([(d, day_value(d, 'raw_total')) for d in calendar], fmt_tokens)}"
        f"</div>"
    )

    headers = ["", "Turns", "Tokens", "Output", "Cost"]

    for title, field, limit, alpha in [
        ("By model", "model_label", None, False),
        ("By surface", "surface", None, False),
        ("By project", "project", 15, False),
        ("Heaviest sessions", "session", 15, False),
        ("By day", "day", None, True),
    ]:
        buckets = aggregate(turns, field)
        label = {"model_label": "Model", "surface": "Where you were working",
                 "project": "Project", "session": "Session", "day": "Day"}[field]
        sections.append(
            f"<h2>{esc(title)}</h2>"
            f"{table([label] + headers[1:], bucket_rows(buckets, limit=limit, sort_alpha=alpha))}"
        )

    for title, field, label in [
        ("By skill", "skill", "Skill"),
        ("By plugin", "plugin", "Plugin"),
        ("By MCP server", "mcp", "MCP server"),
    ]:
        buckets = {k: v for k, v in aggregate(turns, field).items() if k != "(none)"}
        if buckets:
            sections.append(
                f"<h2>{esc(title)}</h2>"
                f"{table([label] + headers[1:], bucket_rows(buckets, limit=20))}"
            )

    unknown_note = ""
    if pricing.unknown_models:
        listed = ", ".join(sorted(pricing.unknown_models)[:6])
        unknown_note = (
            f'<div class="note"><b>{len(pricing.unknown_models)} model(s) had no '
            f"price on file</b> ({esc(listed)}). Their tokens are counted but "
            f"excluded from cost. Add them to <code>pricing.json</code> to fix.</div>"
        )

    busiest_line = ""
    if busiest[0]:
        busiest_line = (
            f" Busiest day was {esc(busiest[0])} at "
            f"{esc(fmt_money(busiest[1]['cost']))}."
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude token usage</title><style>{CSS}</style></head><body>
<div class="wrap">
<h1>Claude token usage</h1>
<p class="sub">{esc(window_label)} · generated {esc(generated)} ·
{fmt_int(stats['files'])} transcript files read</p>

<div class="note">
<b>What this covers.</b> Every turn from Claude Code in the terminal, the editor
extensions, and Cowork — read straight from the transcripts on this machine, so the
token counts are exact.<br>
<b>What it cannot cover.</b> claude.ai in the browser, the desktop chat window, and
the mobile apps keep no transcript on disk and expose no per-conversation usage API.
Those are missing here entirely — check Settings → Usage for them.<br>
<b>Cost is an estimate</b> at list API prices (checked {esc(pricing.checked)}). If you
are on a subscription plan you are not billed per token, so read cost as relative
weight, not as an invoice.{busiest_line}
</div>

{unknown_note}

<div class="tiles">{tile_html}</div>

{''.join(sections)}

<footer>
Scanned: {esc(', '.join(str(r) for r in roots)) or '(nothing found)'}<br>
{fmt_int(stats['lines'])} transcript lines · {fmt_int(stats['duplicates'])} duplicate
requests skipped · {fmt_int(stats['bad_lines'])} unreadable lines<br>
Prices from <a href="{esc(pricing.source)}">the published pricing page</a>.
Cache writes are priced at their real 5-minute or 1-hour rate where the transcript
records which was used.<br>
claude-token-tracker · runs entirely on your machine, sends nothing anywhere.
</footer>
</div></body></html>"""


# --------------------------------------------------------------------------- #
# terminal output
# --------------------------------------------------------------------------- #

def print_summary(turns: list[Turn], stats: dict, pricing: Pricing,
                  window_label: str) -> None:
    grand = totals(turns)
    print()
    print(f"  Claude token usage — {window_label}")
    print("  " + "-" * 52)
    print(f"  Turns              {fmt_int(grand['turns'])}"
          f"   across {fmt_int(len(grand['sessions']))} sessions")
    print(f"  Tokens billed      {fmt_tokens(grand['raw_total'])}")
    print(f"    fresh input      {fmt_tokens(grand['input'])}")
    print(f"    cache reads      {fmt_tokens(grand['cache_read'])}")
    print(f"    cache writes     {fmt_tokens(grand['cache_write'])}")
    print(f"    output           {fmt_tokens(grand['output'])}"
          f"   ({fmt_tokens(grand['thinking'])} thinking)")
    if grand["searches"]:
        print(f"  Web searches       {fmt_int(grand['searches'])}")
    print(f"  Estimated cost     {fmt_money(grand['cost'])}")
    print(f"  Saved by caching   {fmt_money(cache_savings(turns, pricing))}")
    if grand["unpriced"]:
        print(f"  ! {fmt_int(grand['unpriced'])} turns had no price on file")
    print()

    for title, field in [("By model", "model_label"), ("By surface", "surface")]:
        buckets = aggregate(turns, field)
        if not buckets:
            continue
        print(f"  {title}")
        for name, data in sorted(buckets.items(), key=lambda kv: -kv[1]["cost"]):
            print(f"    {name[:34]:<34} {fmt_tokens(data['raw_total']):>9}"
                  f" {fmt_money(data['cost']):>10}")
        print()


def print_session_detail(turns: list[Turn], session_id: str) -> None:
    picked = [t for t in turns if t.session.startswith(session_id)]
    if not picked:
        print(f"No turns found for session starting '{session_id}'.")
        return
    picked.sort(key=lambda t: t.ts or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    print()
    print(f"  Session {picked[0].session}")
    print(f"  {picked[0].project} · {picked[0].surface} · "
          f"{picked[0].model_label}")
    print("  " + "-" * 74)
    print(f"  {'#':>3} {'time':>8} {'in':>8} {'cache rd':>9} {'cache wr':>9}"
          f" {'out':>8} {'cost':>9}")
    running = 0.0
    for index, turn in enumerate(picked, 1):
        running += turn.cost or 0.0
        clock = turn.ts.strftime("%H:%M:%S") if turn.ts else "--"
        flag = " *" if turn.is_sidechain else ""
        print(f"  {index:>3} {clock:>8} {fmt_tokens(turn.input):>8}"
              f" {fmt_tokens(turn.cache_read):>9}"
              f" {fmt_tokens(turn.cache_write_5m + turn.cache_write_1h):>9}"
              f" {fmt_tokens(turn.output):>8} {fmt_money(turn.cost):>9}{flag}")
    print("  " + "-" * 74)
    print(f"  {len(picked)} turns · {fmt_money(running)} total"
          f" · {fmt_money(running / len(picked))} per turn average")
    if any(t.is_sidechain for t in picked):
        print("  * subagent turn")
    print()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="token_report.py",
        description="Token and cost accounting from local Claude transcripts.",
    )
    parser.add_argument("--days", type=int, default=30,
                        help="how many days back to include (default 30)")
    parser.add_argument("--today", action="store_true",
                        help="today only")
    parser.add_argument("--all", action="store_true",
                        help="every transcript, no date limit")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="start date, inclusive")
    parser.add_argument("--until", metavar="YYYY-MM-DD",
                        help="end date, inclusive")
    parser.add_argument("--out", metavar="PATH",
                        help="where to write the HTML dashboard "
                             "(default ~/.claude/token-report.html)")
    parser.add_argument("--no-html", action="store_true",
                        help="skip the dashboard, print the summary only")
    parser.add_argument("--open", dest="do_open", action="store_true",
                        help="open the dashboard in a browser when done")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="emit machine-readable JSON on stdout")
    parser.add_argument("--session", metavar="ID",
                        help="turn-by-turn breakdown for one session (id prefix ok)")
    parser.add_argument("--group-by", metavar="FIELD", choices=sorted(BUCKET_FIELDS),
                        help="print one grouping: " + ", ".join(sorted(BUCKET_FIELDS)))
    parser.add_argument("--root", action="append", metavar="DIR",
                        help="transcript directory to scan (repeatable)")
    parser.add_argument("--pricing", metavar="PATH", default=str(DEFAULT_PRICING),
                        help="pricing table to use")
    parser.add_argument("--no-subagents", action="store_true",
                        help="exclude subagent (sidechain) turns")
    parser.add_argument("--utc", action="store_true",
                        help="bucket days by UTC instead of local time")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    pricing_path = Path(os.path.expanduser(args.pricing))
    if not pricing_path.is_file():
        print(f"Pricing file not found: {pricing_path}", file=sys.stderr)
        return 2
    pricing = Pricing(pricing_path)

    roots = discover_roots(args.root)
    if not roots:
        print(
            "No Claude transcript directory found.\n"
            "Looked for ~/.claude/projects. If your transcripts live elsewhere, "
            "pass --root /path/to/projects.",
            file=sys.stderr,
        )
        return 1

    turns, stats = collect_turns(
        roots, pricing, use_utc=args.utc,
        include_sidechains=not args.no_subagents,
    )

    # date window
    today = dt.datetime.now(dt.timezone.utc).date() if args.utc else dt.date.today()
    start = end = None
    if args.today:
        start = end = today
        window = f"Today ({today.isoformat()})"
    elif args.all:
        window = "All recorded history"
    elif args.since or args.until:
        start = dt.date.fromisoformat(args.since) if args.since else None
        end = dt.date.fromisoformat(args.until) if args.until else None
        window = f"{args.since or 'start'} to {args.until or 'now'}"
    else:
        start = today - dt.timedelta(days=args.days - 1)
        end = today
        window = f"Last {args.days} days"

    if start or end:
        kept = []
        for turn in turns:
            if turn.ts is None:
                continue
            day = turn.ts.date()
            if start and day < start:
                continue
            if end and day > end:
                continue
            kept.append(turn)
        turns = kept

    if not turns:
        print(f"No usage found for: {window}.", file=sys.stderr)
        print(f"Scanned {stats['files']} transcript files under "
              f"{', '.join(str(r) for r in roots)}.", file=sys.stderr)
        return 0

    if args.session:
        print_session_detail(turns, args.session)
        return 0

    if args.as_json:
        def clean(buckets: dict) -> dict:
            return {
                key: {**{k: v for k, v in data.items() if k != "sessions"},
                      "sessions": len(data["sessions"]),
                      "cost": round(data["cost"], 6)}
                for key, data in buckets.items()
            }

        grand = totals(turns)
        payload = {
            "window": window,
            "generated": dt.datetime.now().isoformat(timespec="seconds"),
            "coverage": {
                "included": ["Claude Code (terminal)", "editor extensions", "Cowork"],
                "excluded": ["claude.ai web", "desktop chat UI", "mobile apps"],
                "reason": "those surfaces write no local transcript and expose no "
                          "per-conversation usage API",
            },
            "pricing_checked": pricing.checked,
            "scan": {k: v for k, v in stats.items()},
            "totals": {**{k: v for k, v in grand.items() if k != "sessions"},
                       "sessions": len(grand["sessions"]),
                       "cost": round(grand["cost"], 6),
                       "cache_savings": round(cache_savings(turns, pricing), 6)},
            "by_day": clean(aggregate(turns, "day")),
            "by_model": clean(aggregate(turns, "model_label")),
            "by_surface": clean(aggregate(turns, "surface")),
            "by_project": clean(aggregate(turns, "project")),
            "unpriced_models": sorted(pricing.unknown_models),
        }
        print(json.dumps(payload, indent=2))
        return 0

    if args.group_by:
        field = BUCKET_FIELDS[args.group_by]
        buckets = aggregate(turns, field)
        print()
        print(f"  {args.group_by:<30} {'turns':>7} {'tokens':>10} {'cost':>10}")
        print("  " + "-" * 60)
        for name, data in sorted(buckets.items(), key=lambda kv: -kv[1]["cost"]):
            print(f"  {str(name)[:30]:<30} {fmt_int(data['turns']):>7}"
                  f" {fmt_tokens(data['raw_total']):>10}"
                  f" {fmt_money(data['cost']):>10}")
        print()
        return 0

    print_summary(turns, stats, pricing, window)

    if not args.no_html:
        out_path = Path(os.path.expanduser(
            args.out or str(Path.home() / ".claude" / "token-report.html")
        ))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            render_html(turns, stats, pricing, window, roots), encoding="utf-8"
        )
        print(f"  Dashboard: {out_path}")
        print()
        if args.do_open:
            webbrowser.open(out_path.as_uri())

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
