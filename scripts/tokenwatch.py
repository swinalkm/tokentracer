#!/usr/bin/env python3
"""
tokenwatch — a small always-on-top desktop panel showing Claude token usage
as it happens.

It tails the JSONL transcripts that Claude Code (terminal), the VS Code and
JetBrains extensions, Cowork and the Agent SDK append while they work, prices
every turn from pricing.json, and repaints a compact widget that sits on your
desktop. Turns are counted the moment they land — including the ones Claude
takes on its own in the middle of a long task, and subagent turns.

Coverage: claude.ai in the browser, the Claude desktop chat app and the mobile
apps write no transcript and expose no per-conversation usage API, so they
cannot appear here. See Settings -> Usage for those.

Standard library only; Tkinter ships with Python. Python 3.9+.

    python3 tokenwatch.py                 # toggle the panel on or off
    python3 tokenwatch.py --run           # run in the foreground (for debugging)
    python3 tokenwatch.py --status
    python3 tokenwatch.py --snapshot      # print the live numbers as JSON
    python3 tokenwatch.py --install-app   # macOS: a dockless .app in ~/Applications

Licence: MIT
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from token_report import (  # noqa: E402  (path juggling has to come first)
    DEFAULT_PRICING,
    Pricing,
    Turn,
    discover_roots,
    fmt_money,
    fmt_tokens,
    row_dedupe_key,
    row_usage,
    turn_from_row,
)

APP_NAME = "Claude Token Watch"

# key, the label under the hero, the chip, and the rule: a number of seconds
# for a rolling window, or a named rule.
SCOPE_SPECS = (
    ("5m",      "LAST 5 MINUTES",  "5m",   300),
    ("15m",     "LAST 15 MINUTES", "15m",  900),
    ("1h",      "LAST HOUR",       "1h",   3600),
    ("3h",      "LAST 3 HOURS",    "3h",   10800),
    ("24h",     "LAST 24 HOURS",   "24h",  86400),
    ("today",   "TODAY",           "day",  "today"),
    ("session", "THIS SESSION",    "sess", "session"),
    ("window",  "WHOLE WINDOW",    "all",  "window"),
)
SCOPES = tuple(spec[0] for spec in SCOPE_SPECS)
SCOPE_LABELS = {spec[0]: spec[1] for spec in SCOPE_SPECS}
SCOPE_CHIPS = {spec[0]: spec[2] for spec in SCOPE_SPECS}
SCOPE_RULES = {spec[0]: spec[3] for spec in SCOPE_SPECS}
SPARK_BARS = 34
MODEL_ROWS = 3
LIVE_SECONDS = 25          # a turn this recent means "live"
FOCUS_SECONDS = 300        # how far back to look for the session in use
DEFAULT_WINDOW_DAYS = 7    # how much history the panel holds in memory


# --------------------------------------------------------------------------- #
# paths and persisted state
# --------------------------------------------------------------------------- #

def config_home() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(os.path.expanduser(env))
    return Path(os.path.expanduser("~")) / ".claude"


STATE_DIR = config_home() / "tokenwatch"
PID_FILE = STATE_DIR / "tokenwatch.pid"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "tokenwatch.log"

# Set TOKENWATCH_DEBUG=1 to have the panel narrate each tick into its log.
DEBUG = os.environ.get("TOKENWATCH_DEBUG") == "1"


def _log(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


DEFAULT_STATE = {
    "x": None,
    "y": None,
    "scope": "today",
    "collapsed": False,
    "topmost": True,
    "autostart": False,
}


def load_state() -> dict:
    state = dict(DEFAULT_STATE)
    try:
        stored = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if isinstance(stored, dict):
        for key in DEFAULT_STATE:
            if key in stored:
                state[key] = stored[key]
    return state


def save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# the watcher: incremental transcript tailing
# --------------------------------------------------------------------------- #

class Watcher:
    """Tails every transcript under `roots` and keeps a window of priced turns.

    Reading is incremental: each file's consumed byte offset is remembered, and
    only whole lines are parsed, so a transcript being appended to mid-write is
    picked up on the next poll rather than corrupting a turn.
    """

    def __init__(self, roots: list[Path], pricing: Pricing,
                 include_sidechains: bool = True,
                 window_days: int = DEFAULT_WINDOW_DAYS):
        self.roots = roots
        self.pricing = pricing
        self.include_sidechains = include_sidechains
        self.window_days = max(1, window_days)
        self.offsets: dict[Path, int] = {}
        self.seen: set[str] = set()
        self.turns: list[Turn] = []
        self.last_scan = 0.0
        self.files: list[Path] = []
        self.errors = 0

    # -- window ----------------------------------------------------------- #

    def window_start(self) -> dt.datetime:
        now = dt.datetime.now().astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight - dt.timedelta(days=self.window_days - 1)

    def _trim(self) -> None:
        """Drop turns that have aged out, so a long-running panel stays small."""
        cutoff = self.window_start()
        if self.turns and self.turns[0].ts and self.turns[0].ts >= cutoff:
            return
        self.turns = [t for t in self.turns if t.ts is None or t.ts >= cutoff]

    # -- file discovery --------------------------------------------------- #

    def _discover(self) -> list[Path]:
        found: list[Path] = []
        for root in self.roots:
            try:
                found.extend(root.rglob("*.jsonl"))
            except OSError:
                self.errors += 1
        return found

    # -- reading ---------------------------------------------------------- #

    def _new_lines(self, path: Path) -> list[str]:
        """Return complete unread lines, advancing the file's offset."""
        try:
            size = path.stat().st_size
        except OSError:
            return []
        offset = self.offsets.get(path, 0)
        if size < offset:      # truncated or replaced — start over
            offset = 0
        if size == offset:
            return []
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                chunk = handle.read(size - offset)
        except OSError:
            self.errors += 1
            return []
        cut = chunk.rfind(b"\n")
        if cut == -1:
            return []          # only a partial line so far; wait for the rest
        self.offsets[path] = offset + cut + 1
        text = chunk[:cut].decode("utf-8", "replace")
        return [line for line in text.split("\n") if line.strip()]

    def _ingest(self, lines: list[str], fallback_session: str,
                cutoff: dt.datetime | None) -> list[Turn]:
        fresh: list[Turn] = []
        for line in lines:
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            usage = row_usage(row)
            if usage is None:
                continue
            key = row_dedupe_key(row)
            if key:
                if key in self.seen:
                    continue
                self.seen.add(key)
            if row.get("isSidechain") and not self.include_sidechains:
                continue
            turn = turn_from_row(
                row, usage, self.pricing, use_utc=False,
                fallback_session=fallback_session,
            )
            if cutoff is not None and turn.ts is not None and turn.ts < cutoff:
                continue
            fresh.append(turn)
        return fresh

    def prime(self) -> int:
        """First pass: load the window's history and seek every file to its end.

        Files untouched since the window opened cannot hold a turn inside it, so
        they are skipped and simply marked as read.
        """
        cutoff = self.window_start()
        cutoff_epoch = cutoff.timestamp()
        self.files = self._discover()
        loaded: list[Turn] = []
        for path in sorted(self.files):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff_epoch:
                self.offsets[path] = stat.st_size     # read, but not parsed
                continue
            loaded.extend(self._ingest(
                self._new_lines(path), path.stem, cutoff
            ))
        loaded.sort(key=lambda t: t.ts or dt.datetime.min.replace(
            tzinfo=dt.timezone.utc))
        self.turns = loaded
        self.last_scan = time.time()
        return len(loaded)

    def poll(self, rescan_every: float = 5.0) -> list[Turn]:
        """Read whatever has been appended since the last call."""
        now = time.time()
        if now - self.last_scan >= rescan_every or not self.files:
            self.files = self._discover()
            self.last_scan = now
        fresh: list[Turn] = []
        for path in self.files:
            lines = self._new_lines(path)
            if lines:
                fresh.extend(self._ingest(lines, path.stem, None))
        if fresh:
            fresh.sort(key=lambda t: t.ts or dt.datetime.min.replace(
                tzinfo=dt.timezone.utc))
            self.turns.extend(fresh)
            self._trim()
        return fresh

    # -- aggregation ------------------------------------------------------ #

    def active_session(self) -> str | None:
        for turn in reversed(self.turns):
            if turn.session:
                return turn.session
        return None

    def _scope_turns(self, scope: str) -> list[Turn]:
        rule = SCOPE_RULES.get(scope, "today")
        if rule == "window":
            return self.turns
        if rule == "session":
            session = self.active_session()
            if not session:
                return []
            return [t for t in self.turns if t.session == session]
        if rule == "today":
            today = dt.datetime.now().astimezone().date()
            return [t for t in self.turns if t.ts and t.ts.date() == today]
        # A rolling window, counted back from now.
        cutoff = dt.datetime.now().astimezone() - dt.timedelta(seconds=rule)
        return [t for t in self.turns if t.ts and t.ts >= cutoff]

    def _saved_by_cache(self, turns: list[Turn]) -> float:
        """What the cache reads would have cost at full input price, less what
        they actually cost."""
        saved = 0.0
        for turn in turns:
            if not turn.priced or not turn.cache_read:
                continue
            rates = self.pricing.rates(turn.model)
            if rates:
                saved += turn.cache_read / 1e6 * (
                    rates["input"] - rates["cache_read"]
                )
        return saved

    def snapshot(self, scope: str = "today") -> dict:
        scope = scope if scope in SCOPES else "today"
        turns = self._scope_turns(scope)
        now = dt.datetime.now().astimezone()

        agg = {
            "tokens": 0, "cost": 0.0, "input": 0, "output": 0, "thinking": 0,
            "cache_read": 0, "cache_write": 0, "unpriced": 0,
            "sidechain_turns": 0, "sidechain_cost": 0.0,
        }
        models: dict[str, dict] = defaultdict(
            lambda: {"tokens": 0, "cost": 0.0, "turns": 0}
        )
        sessions: set[str] = set()

        for turn in turns:
            agg["tokens"] += turn.raw_total or 0
            agg["cost"] += turn.cost or 0.0
            agg["input"] += turn.input or 0
            agg["output"] += turn.output or 0
            agg["thinking"] += turn.thinking or 0
            agg["cache_read"] += turn.cache_read or 0
            agg["cache_write"] += (turn.cache_write_5m or 0) + \
                                  (turn.cache_write_1h or 0)
            if not turn.priced:
                agg["unpriced"] += 1
            if turn.is_sidechain:
                agg["sidechain_turns"] += 1
                agg["sidechain_cost"] += turn.cost or 0.0
            if turn.session:
                sessions.add(turn.session)
            bucket = models[turn.model_label or "unknown"]
            bucket["tokens"] += turn.raw_total or 0
            bucket["cost"] += turn.cost or 0.0
            bucket["turns"] += 1

        ranked = sorted(
            ({"label": k, **v} for k, v in models.items()),
            key=lambda m: (-m["cost"], -m["tokens"]),
        )

        # Which session is actually doing the work. Naming the *last* turn's
        # surface lets one stray turn from an unrelated session relabel the
        # panel — a single background terminal turn made it read "Terminal"
        # in the middle of a long VS Code session.
        focus_window = now - dt.timedelta(seconds=FOCUS_SECONDS)
        counts: dict[str, int] = {}
        for turn in self.turns:
            if turn.ts and turn.ts >= focus_window and turn.session:
                counts[turn.session] = counts.get(turn.session, 0) + 1
        focus_session = (max(counts, key=lambda k: counts[k]) if counts
                         else (self.turns[-1].session if self.turns else None))
        focus_turn = next(
            (t for t in reversed(self.turns) if t.session == focus_session), None
        )
        focus = None
        if focus_turn is not None:
            focus = {
                "surface": focus_turn.surface or "",
                "project": focus_turn.project or "",
                "session": focus_turn.session or "",
                # Other sessions are contributing to these totals too; say so
                # rather than quietly folding them in.
                "others": max(0, len(counts) - 1),
            }

        last = self.turns[-1] if self.turns else None
        idle = None
        last_info = None
        if last is not None:
            if last.ts is not None:
                idle = max(0.0, (now - last.ts).total_seconds())
            last_info = {
                "tokens": last.raw_total or 0,
                "cost": last.cost or 0.0,
                "model": last.model_label or "unknown",
                "surface": last.surface or "",
                "project": last.project or "",
                "session": last.session or "",
                "sidechain": bool(last.is_sidechain),
                "priced": bool(last.priced),
            }

        span = ""
        if len(turns) > 1:      # a single turn spans nothing worth printing
            first_ts = next((t.ts for t in turns if t.ts), None)
            last_ts = next((t.ts for t in reversed(turns) if t.ts), None)
            if first_ts and last_ts:
                span = human_duration((last_ts - first_ts).total_seconds())

        return {
            "scope": scope,
            "scope_label": (f"LAST {self.window_days} DAYS"
                            if scope == "window" else SCOPE_LABELS[scope]),
            "turns": len(turns),
            "sessions": len(sessions),
            "span": span,
            "saved": self._saved_by_cache(turns),
            "spark": [t.cost or 0.0 for t in turns[-SPARK_BARS:]],
            "models": ranked[:MODEL_ROWS],
            "model_count": len(ranked),
            "last": last_info,
            "focus": focus,
            "idle_seconds": idle,
            "live": idle is not None and idle <= LIVE_SECONDS,
            "tracked_turns": len(self.turns),
            "errors": self.errors,
            **agg,
        }


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if secs < 10 else f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h"


# --------------------------------------------------------------------------- #
# the panel
# --------------------------------------------------------------------------- #

BORDER = "#2c2f3b"
BG = "#15161c"
HEADER = "#1e2029"
TEXT = "#e9eaef"
MUTED = "#8b90a0"
DIM = "#5e6373"
ACCENT = "#e0855e"
ACCENT_DIM = "#6d4535"
LIVE = "#69c58a"
BAR = "#3d4252"

# The panel is narrow, so surfaces get a short name.
SHORT_SURFACE = {
    "Terminal (Claude Code)": "Terminal",
    "VS Code extension": "VS Code",
    "JetBrains extension": "JetBrains",
    "Cowork (desktop)": "Cowork",
    "Claude Code (desktop app)": "Desktop",
    "Claude Code on the web": "Web",
    "Agent SDK": "SDK",
    "Agent SDK (Python)": "SDK",
    "Agent SDK (TypeScript)": "SDK",
    "Claude in Slack": "Slack",
    "Scheduled task": "Scheduled",
    "(unrecorded)": "unknown",
}


def _pick_family(available: set[str], preferred: list[str], fallback: str) -> str:
    lowered = {name.lower() for name in available}
    for name in preferred:
        if name.lower() in lowered:
            return name
    return fallback


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def _ellipsis(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


class Panel:
    """The widget itself. Repaints on a timer; every number comes from Watcher."""

    WIDTH = 320
    PAD = 14

    def __init__(self, watcher: Watcher, state: dict, args):
        import tkinter as tk
        from tkinter import font as tkfont

        self.tk = tk
        self.watcher = watcher
        self.state = state
        self.args = args
        self.interval = max(250, int(args.interval * 1000))
        self.scope = state.get("scope") if state.get("scope") in SCOPES else "today"
        self.collapsed = bool(state.get("collapsed"))
        self.topmost = bool(state.get("topmost", True))
        self._flash_job = None
        self._ticks = 0
        self._fonts: dict = {}
        self._drag = (0, 0)
        self._stopping = False

        root = tk.Tk()
        self.root = root
        root.title(APP_NAME)
        root.configure(bg=BORDER)
        root.resizable(False, False)
        if not args.decorated:
            try:
                root.overrideredirect(True)
            except tk.TclError:
                pass
        try:
            root.attributes("-alpha", args.opacity)
        except tk.TclError:
            pass
        self._apply_topmost()

        families = set(tkfont.families(root))
        mono = _pick_family(families, [
            "SF Mono", "Menlo", "DejaVu Sans Mono", "Consolas", "Courier New",
        ], "Courier")
        sans = _pick_family(families, [
            "SF Pro Text", "Helvetica Neue", "Segoe UI", "Ubuntu",
            "DejaVu Sans", "Helvetica",
        ], "Helvetica")
        self.f_title = (sans, 11, "bold")
        self.f_hero = (mono, 21, "bold")
        self.f_body = (sans, 10)
        self.f_small = (sans, 9)
        self.f_caps = (sans, 8, "bold")
        self.f_chip = (sans, 9, "bold")
        self.f_num = (mono, 10)
        self.f_btn = (sans, 12)

        self._build()
        self._bind_keys()
        self.refresh(first=True)
        self._place()
        self._tick()

    # -- construction ----------------------------------------------------- #

    def _fit(self, label, text: str, budget: int | None = None) -> str:
        """Trim `text` until it actually fits, in pixels.

        Character counts are not good enough here: the panel has a fixed
        width, and one long line (a subagent turn, a wordy model name) would
        otherwise widen the whole window mid-session.
        """
        budget = budget if budget is not None else self.WIDTH - 2 * self.PAD
        key = str(label.cget("font"))
        font = self._fonts.get(key)
        if font is None:
            from tkinter import font as tkfont
            font = self._fonts[key] = tkfont.Font(font=key)
        if font.measure(text) <= budget:
            return text
        while text and font.measure(text + "…") > budget:
            text = text[:-1]
        return text + "…"

    def _text(self, parent, text="", font=None, fg=TEXT, bg=BG, **kw):
        # Horizontal chrome zeroed so a label's requested width is exactly
        # its text width: _fit budgets in pixels, and Tk's default padx would
        # otherwise let a full line push the window wider than WIDTH.
        # Vertical padding stays — it only affects the rhythm.
        return self.tk.Label(
            parent, text=text, font=font or self.f_body, fg=fg, bg=bg,
            anchor=kw.pop("anchor", "w"), justify=kw.pop("justify", "left"),
            padx=kw.pop("padx", 0), pady=kw.pop("pady", 1),
            bd=0, highlightthickness=0, **kw
        )

    def _build(self) -> None:
        tk = self.tk
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        self.outer = outer
        # Pins the width while letting height follow the content.
        tk.Frame(outer, bg=BG, width=self.WIDTH - 2, height=1).pack()

        # header -------------------------------------------------------- #
        header = tk.Frame(outer, bg=HEADER)
        header.pack(fill="x")
        self.header = header
        inner = tk.Frame(header, bg=HEADER)
        inner.pack(fill="x", padx=self.PAD - 4, pady=6)

        self.title = self._text(inner, "Claude tokens", self.f_title,
                                TEXT, HEADER)
        self.title.pack(side="left")

        self.btn_close = self._text(inner, "✕", self.f_btn, DIM, HEADER,
                                    cursor="hand2")
        self.btn_close.pack(side="right", padx=(6, 0))
        self.btn_collapse = self._text(inner, "–", self.f_btn, DIM, HEADER,
                                       cursor="hand2")
        self.btn_collapse.pack(side="right")

        self.btn_close.bind("<Button-1>", lambda _e: self.quit())
        self.btn_collapse.bind("<Button-1>", lambda _e: self.toggle_collapse())
        for widget in (self.btn_close, self.btn_collapse):
            widget.bind("<Enter>", lambda e: e.widget.configure(fg=TEXT))
            widget.bind("<Leave>", lambda e: e.widget.configure(fg=DIM))

        for widget in (header, inner, self.title):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="x", padx=self.PAD, pady=(9, 11))
        self.body = body

        # status -------------------------------------------------------- #
        status = tk.Frame(body, bg=BG)
        status.pack(fill="x")
        self.dot = self._text(status, "●", (self.f_small[0], 9), DIM)
        self.dot.pack(side="left", padx=(0, 5))
        self.status = self._text(status, "starting…", self.f_small, MUTED)
        self.status.pack(side="left")

        # hero ---------------------------------------------------------- #
        hero = tk.Frame(body, bg=BG)
        hero.pack(fill="x", pady=(8, 0))
        self.hero_tokens = self._text(hero, "0", self.f_hero, TEXT)
        self.hero_tokens.pack(side="left")
        self.hero_cost = self._text(hero, "$0.00", self.f_hero, ACCENT,
                                    anchor="e")
        self.hero_cost.pack(side="right")

        legend = tk.Frame(body, bg=BG)
        legend.pack(fill="x", pady=(1, 0))
        self.scope_label = self._text(legend, "TODAY", self.f_caps, DIM,
                                      cursor="hand2")
        self.scope_label.pack(side="left")
        self.hero_hint = self._text(legend, "tokens · cost", self.f_caps,
                                    DIM, anchor="e")
        self.hero_hint.pack(side="right")
        for widget in (self.scope_label, self.hero_tokens, self.hero_cost):
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>", lambda _e: self.cycle_scope())

        # scope chips ---------------------------------------------------- #
        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", pady=(9, 0))
        self.chip_row = row
        self.chips: dict = {}
        for key in SCOPES:
            chip = self._text(row, SCOPE_CHIPS[key], self.f_chip, DIM,
                              padx=4, pady=2, cursor="hand2")
            chip.pack(side="left", padx=(0, 2))
            chip.bind("<Button-1>", lambda _e, k=key: self.set_scope(k))
            chip.bind("<Enter>", self._chip_enter)
            chip.bind("<Leave>", self._chip_leave)
            self.chips[key] = chip

        # last turn ----------------------------------------------------- #
        self.delta = self._text(body, "waiting for a turn…", self.f_num,
                                MUTED)
        self.delta.pack(fill="x", pady=(9, 0))

        # sparkline ----------------------------------------------------- #
        self.spark = tk.Canvas(
            body, width=self.WIDTH - 2 * self.PAD, height=30, bg=BG,
            highlightthickness=0, bd=0,
        )
        self.spark.pack(fill="x", pady=(7, 0))
        self.spark_caption = self._text(body, "cost per turn", self.f_caps, DIM)
        self.spark_caption.pack(fill="x")

        # models -------------------------------------------------------- #
        self.models = tk.Frame(body, bg=BG)
        self.models.pack(fill="x", pady=(10, 0))
        self.models.columnconfigure(0, weight=1)
        self.model_rows = []
        for index in range(MODEL_ROWS):
            name = self._text(self.models, "", self.f_small, TEXT)
            toks = self._text(self.models, "", self.f_num, MUTED, anchor="e")
            cost = self._text(self.models, "", self.f_num, ACCENT, anchor="e")
            name.grid(row=index, column=0, sticky="w", pady=1)
            toks.grid(row=index, column=1, sticky="e", padx=(8, 10))
            cost.grid(row=index, column=2, sticky="e")
            self.model_rows.append((name, toks, cost))

        # splits + footer ----------------------------------------------- #
        self.splits = self._text(body, "", self.f_small, MUTED)
        self.splits.pack(fill="x", pady=(10, 0))
        self.footer = self._text(body, "", self.f_small, DIM)
        self.footer.pack(fill="x", pady=(3, 0))

        self._menu = self._build_menu()
        for widget in (outer, header, inner, body, self.title, self.status,
                       self.footer, self.splits):
            widget.bind("<Button-3>", self._popup)
            widget.bind("<Button-2>", self._popup)
            widget.bind("<Control-Button-1>", self._popup)

        self._apply_collapse()

    def _build_menu(self):
        menu = self.tk.Menu(self.root, tearoff=0)
        for key in SCOPES:
            label = (f"Last {self.watcher.window_days} days" if key == "window"
                     else SCOPE_LABELS[key].capitalize())
            menu.add_command(label=label,
                             command=lambda k=key: self.set_scope(k))
        menu.add_separator()
        self._topmost_var = self.tk.BooleanVar(value=self.topmost)
        menu.add_checkbutton(label="Always on top", variable=self._topmost_var,
                             command=self.toggle_topmost)
        menu.add_command(label="Copy summary", command=self.copy_summary)
        menu.add_command(label="Open full report…",
                         command=self.open_report)
        menu.add_separator()
        menu.add_command(label="Quit", command=self.quit)
        return menu

    def _popup(self, event):
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _bind_keys(self) -> None:
        self.root.bind("<Command-w>", lambda _e: self.quit())
        self.root.bind("<Control-w>", lambda _e: self.quit())
        self.root.bind("<Command-q>", lambda _e: self.quit())
        self.root.bind("<space>", lambda _e: self.cycle_scope())
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # -- geometry --------------------------------------------------------- #

    def _place(self) -> None:
        """Restore the last position, else settle into the bottom-right corner.

        Sizes come from the *requested* geometry: the real one is not known
        until the window is mapped, and reading it too early puts the panel
        somewhere arbitrary.
        """
        root = self.root
        root.update_idletasks()
        width = root.winfo_reqwidth() or self.WIDTH
        height = root.winfo_reqheight() or 320
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()

        x, y = self.state.get("x"), self.state.get("y")
        remembered = (
            isinstance(x, int) and isinstance(y, int)
            and -8 <= x <= screen_w - 80 and 0 <= y <= screen_h - 40
        )
        if not remembered:
            x = screen_w - width - 28
            y = screen_h - height - 90
        # Keep it on screen even if the display arrangement changed.
        x = max(0, min(int(x), max(0, screen_w - width)))
        y = max(0, min(int(y), max(0, screen_h - height)))
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.update_idletasks()

    def _drag_start(self, event) -> None:
        self._drag = (event.x_root - self.root.winfo_x(),
                      event.y_root - self.root.winfo_y())

    def _drag_move(self, event) -> None:
        x = event.x_root - self._drag[0]
        y = event.y_root - self._drag[1]
        self.root.geometry(f"+{x}+{y}")

    def _apply_topmost(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.topmost))
        except self.tk.TclError:
            pass

    def _apply_collapse(self) -> None:
        detail = (
            (self.spark, {"pady": (7, 0)}),
            (self.spark_caption, {}),
            (self.models, {"pady": (10, 0)}),
            (self.splits, {"pady": (10, 0)}),
            (self.footer, {"pady": (3, 0)}),
        )
        for widget, opts in detail:
            widget.pack_forget()
            if not self.collapsed:
                widget.pack(fill="x", **opts)
        self.btn_collapse.configure(text="+" if self.collapsed else "–")
        self.root.update_idletasks()
        # The window was given an explicit size by _place, so it will not
        # shrink on its own when the detail rows go away.
        self.root.geometry(f"{self.root.winfo_reqwidth()}x"
                           f"{self.root.winfo_reqheight()}")

    # -- actions ---------------------------------------------------------- #

    def _paint_chips(self) -> None:
        for key, chip in self.chips.items():
            if key == self.scope:
                chip.configure(fg=ACCENT, bg=HEADER)
            else:
                chip.configure(fg=DIM, bg=BG)

    def _chip_enter(self, event) -> None:
        if event.widget.cget("fg") != ACCENT:      # leave the selected one be
            event.widget.configure(fg=TEXT)

    def _chip_leave(self, event) -> None:
        if event.widget.cget("fg") != ACCENT:
            event.widget.configure(fg=DIM)

    def toggle_collapse(self) -> None:
        self.collapsed = not self.collapsed
        self._apply_collapse()

    def toggle_topmost(self) -> None:
        self.topmost = bool(self._topmost_var.get())
        self._apply_topmost()

    def set_scope(self, scope: str) -> None:
        if scope in SCOPES:
            self.scope = scope
            self.refresh()

    def cycle_scope(self) -> None:
        self.set_scope(SCOPES[(SCOPES.index(self.scope) + 1) % len(SCOPES)])

    def copy_summary(self) -> None:
        snap = self.watcher.snapshot(self.scope)
        lines = [
            f"Claude tokens — {snap['scope_label'].lower()}",
            f"  {fmt_tokens(snap['tokens'])} tokens, "
            f"{fmt_money(snap['cost'])} across {_plural(snap['turns'], 'turn')}",
        ]
        for model in snap["models"]:
            lines.append(f"  {model['label']}: {fmt_tokens(model['tokens'])}, "
                         f"{fmt_money(model['cost'])}")
        lines.append(f"  saved by caching: {fmt_money(snap['saved'])}")
        lines.append("  Cost is an estimate at list API prices.")
        text = "\n".join(lines)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._notify("copied to clipboard")

    def open_report(self) -> None:
        script = str(SCRIPT_DIR / "token_report.py")
        try:
            subprocess.Popen(
                [sys.executable, script, "--days", "30", "--open"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=(os.name != "nt"),
            )
            self._notify("opening full report…")
        except OSError as exc:
            self._notify(f"could not open report: {exc}")

    def _notify(self, message: str) -> None:
        self.status.configure(text=_ellipsis(message, 44), fg=ACCENT)
        self.root.after(2200, self.refresh)

    def quit(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        # Re-read first and write back only the keys the panel owns. Settings
        # changed elsewhere while it was up (autostart, say) must survive.
        try:
            x, y = self.root.winfo_x(), self.root.winfo_y()
        except self.tk.TclError:
            x, y = self.state.get("x"), self.state.get("y")
        persisted = load_state()
        persisted.update({
            "x": x,
            "y": y,
            "scope": self.scope,
            "collapsed": self.collapsed,
            "topmost": self.topmost,
        })
        save_state(persisted)
        clear_pid(os.getpid())
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass

    # -- painting --------------------------------------------------------- #

    def _tick(self) -> None:
        if self._stopping:
            return
        try:
            fresh = self.watcher.poll()
        except Exception:                      # a bad poll must not kill the UI
            fresh = []
        self.refresh(flash=bool(fresh))
        self._ticks += 1
        if DEBUG:
            _log(f"tick {self._ticks} fresh={len(fresh)} "
                 f"held={len(self.watcher.turns)} "
                 f"hero={self.hero_tokens.cget('text')}/"
                 f"{self.hero_cost.cget('text')} "
                 f"mapped={bool(self.root.winfo_ismapped())}")
        self.root.after(self.interval, self._tick)

    def refresh(self, first: bool = False, flash: bool = False) -> None:
        if self._stopping:
            return
        snap = self.watcher.snapshot(self.scope)

        # status line
        if snap["live"]:
            self.dot.configure(fg=LIVE)
            state_text = "live"
        elif snap["idle_seconds"] is None:
            self.dot.configure(fg=DIM)
            state_text = "no turns recorded"
        else:
            self.dot.configure(fg=DIM)
            state_text = f"idle {human_duration(snap['idle_seconds'])}"
        bits = [state_text]
        if snap["idle_seconds"] is not None and snap["live"]:
            # Ticks every second, so the panel is visibly alive even when the
            # totals have not moved.
            bits.append(f"{int(snap['idle_seconds'])}s ago")
        if snap["focus"]:
            surface = snap["focus"]["surface"]
            bits.append(SHORT_SURFACE.get(surface, _ellipsis(surface, 14)))
            if snap["focus"]["project"]:
                bits.append(_ellipsis(snap["focus"]["project"], 16))
            if snap["focus"]["others"]:
                bits.append(f"+{snap['focus']['others']}")
        self.status.configure(text=self._fit(self.status, " · ".join(bits)),
                              fg=MUTED)

        # hero
        self.hero_tokens.configure(text=fmt_tokens(snap["tokens"]))
        self.hero_cost.configure(text=fmt_money(snap["cost"]))
        self.scope_label.configure(text=snap["scope_label"])
        self._paint_chips()

        # last turn
        last = snap["last"]
        if last:
            parts = [f"+{fmt_tokens(last['tokens'])}"]
            parts.append(fmt_money(last["cost"]) if last["priced"] else "unpriced")
            if last["sidechain"]:
                parts.append("subagent")
            # Model last, so it is what gets trimmed if the line is tight.
            parts.append(last["model"])
            self.delta.configure(
                text=self._fit(self.delta, "last turn  " + " · ".join(parts)),
                fg=ACCENT if flash else MUTED,
            )
            if flash:
                if self._flash_job:
                    self.root.after_cancel(self._flash_job)
                self._flash_job = self.root.after(
                    1100, lambda: self.delta.configure(fg=MUTED)
                )
        else:
            self.delta.configure(text="waiting for a turn…", fg=DIM)

        self._draw_spark(snap["spark"])
        self.spark_caption.configure(
            text=f"cost per turn · last {len(snap['spark'])}"
            if snap["spark"] else "cost per turn"
        )

        # models
        for index, (name, toks, cost) in enumerate(self.model_rows):
            if index < len(snap["models"]):
                model = snap["models"][index]
                name.configure(text=_ellipsis(model["label"], 16))
                toks.configure(text=fmt_tokens(model["tokens"]))
                cost.configure(text=fmt_money(model["cost"]))
            else:
                name.configure(text="")
                toks.configure(text="")
                cost.configure(text="")

        # splits
        self.splits.configure(text=self._fit(self.splits, " · ".join([
            f"cache {fmt_tokens(snap['cache_read'])}",
            f"write {fmt_tokens(snap['cache_write'])}",
            f"out {fmt_tokens(snap['output'])}",
            f"in {fmt_tokens(snap['input'])}",
        ])))

        # footer
        foot = [_plural(snap["turns"], "turn")]
        if snap["scope"] != "session" and snap["sessions"] > 1:
            foot.append(_plural(snap["sessions"], "session"))
        if snap["span"]:
            foot.append(snap["span"])
        if snap["saved"] >= 0.01:
            foot.append(f"cache saved {fmt_money(snap['saved'])}")
        if snap["unpriced"]:
            foot.append(f"{snap['unpriced']} unpriced")
        self.footer.configure(text=self._fit(self.footer, " · ".join(foot)))

        # Flush the repaint now rather than waiting for the window server to
        # get around to a background application.
        try:
            self.root.update_idletasks()
        except self.tk.TclError:
            pass

    def _draw_spark(self, values: list[float]) -> None:
        canvas = self.spark
        canvas.delete("all")
        width = int(canvas["width"])
        height = int(canvas["height"])
        if not values:
            canvas.create_line(0, height - 1, width, height - 1, fill=BORDER)
            return
        peak = max(values) or 1.0
        count = len(values)
        gap = 2.0
        bar = max(2.0, (width - gap * (count - 1)) / count)
        for index, value in enumerate(values):
            tall = max(1.0, (value / peak) * (height - 2))
            left = index * (bar + gap)
            colour = ACCENT if index == count - 1 else BAR
            canvas.create_rectangle(
                left, height - tall, left + bar, height,
                fill=colour, width=0,
            )

    def run(self) -> None:
        self.root.mainloop()


# --------------------------------------------------------------------------- #
# process control
# --------------------------------------------------------------------------- #

def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_is_ours(pid: int) -> bool:
    """Guard against a recycled PID belonging to some unrelated process."""
    if os.name == "nt":
        return True
    try:
        out = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return _is_panel_command(out.stdout.strip())


def read_pid() -> int | None:
    try:
        data = json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = data.get("pid") if isinstance(data, dict) else None
    if not isinstance(pid, int):
        return None
    if not _pid_alive(pid) or not _pid_is_ours(pid):
        clear_pid()
        return None
    return pid


def write_pid(pid: int) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(json.dumps({
            "pid": pid,
            "started": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "script": str(Path(__file__).resolve()),
        }), encoding="utf-8")
    except OSError:
        pass


def clear_pid(only_if: int | None = None) -> None:
    """Forget the registered panel. With `only_if`, forget it only when the
    registration is that pid — so a stray foreground `--run` exiting cannot
    deregister the panel someone else has on screen."""
    if only_if is not None:
        try:
            data = json.loads(PID_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("pid") != only_if:
            return
    try:
        PID_FILE.unlink()
    except OSError:
        pass


def _is_panel_command(command: str) -> bool:
    """True only for `<python> <this script> … --run`.

    Matching on whole arguments matters: a shell command that merely mentions
    this script's path — a grep, an editor, this very sweep — must never be
    mistaken for a panel and killed.
    """
    parts = command.split()
    if len(parts) < 3:
        return False
    if "python" not in os.path.basename(parts[0]).lower():
        return False
    return str(Path(__file__).resolve()) in parts[1:] and "--run" in parts[1:]


def stray_panels(exclude: int | None = None) -> list[int]:
    """Panel processes started from this script that nothing has registered."""
    if os.name == "nt":
        return []
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,command="],
                             capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.stdout.splitlines():
        head, _, command = line.strip().partition(" ")
        if not head.isdigit():
            continue
        pid = int(head)
        if pid in (os.getpid(), exclude):
            continue
        if _is_panel_command(command):
            found.append(pid)
    return found


def _panel_interpreter() -> str:
    """The interpreter to launch the panel with.

    On Windows, pythonw.exe runs a GUI program without allocating a console;
    launching through python.exe leaves an empty console window sitting behind
    the panel for as long as it is open.
    """
    if os.name != "nt":
        return sys.executable
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        windowed = exe.with_name("pythonw.exe")
        if windowed.exists():
            return str(windowed)
    return sys.executable


def _panel_argv(args) -> list[str]:
    """The flags the detached panel process needs to match this invocation."""
    passthrough = ["--run"]
    if args.interval != 1.0:
        passthrough += ["--interval", str(args.interval)]
    if args.opacity != 0.96:
        passthrough += ["--opacity", str(args.opacity)]
    if args.decorated:
        passthrough += ["--decorated"]
    if args.days != DEFAULT_WINDOW_DAYS:
        passthrough += ["--days", str(args.days)]
    if args.no_subagents:
        passthrough += ["--no-subagents"]
    if args.debug:
        passthrough += ["--debug"]
    if args.pricing != str(DEFAULT_PRICING):
        passthrough += ["--pricing", args.pricing]
    for root in args.root or []:
        passthrough += ["--root", root]
    return passthrough


def start_panel(args, quiet: bool = False) -> int:
    running = read_pid()
    if running:
        if not quiet:
            print(f"{APP_NAME} is already showing (pid {running}).")
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [_panel_interpreter(), str(Path(__file__).resolve())] + _panel_argv(args)
    kwargs: dict = {"stdin": subprocess.DEVNULL}
    try:
        log = open(LOG_FILE, "ab", buffering=0)
    except OSError:
        log = subprocess.DEVNULL
    kwargs["stdout"] = log
    kwargs["stderr"] = log
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        print(f"could not launch the panel: {exc}", file=sys.stderr)
        return 1

    # Register it here rather than from inside the child: the pid is already
    # known, so a toggle that arrives a moment later cannot miss it and open
    # a second panel.
    write_pid(proc.pid)

    # Long enough to catch a panel that dies on startup (no display, no Tk).
    deadline = time.time() + 1.5
    while time.time() < deadline:
        if proc.poll() is not None:
            clear_pid(proc.pid)
            print(f"{APP_NAME} exited immediately (code {proc.returncode}).",
                  file=sys.stderr)
            _print_log_tail()
            return 1
        time.sleep(0.15)
    if not quiet:
        print(f"{APP_NAME} is on your desktop (pid {proc.pid}). "
              f"Close it with the ✕ on the panel, or /tokens-watch off.")
    return 0


def _print_log_tail(lines: int = 12) -> None:
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    tail = [ln for ln in text.splitlines() if ln.strip()][-lines:]
    if tail:
        print(f"--- {LOG_FILE} ---", file=sys.stderr)
        for line in tail:
            print(f"  {line}", file=sys.stderr)


def _terminate(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        return
    for _ in range(20):
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def stop_panel(quiet: bool = False) -> int:
    pid = read_pid()
    # Any panel this script left behind gets closed too, so "off" always
    # means no panel is left on screen.
    targets = [p for p in [pid] if p] + stray_panels(exclude=pid)
    if not targets:
        clear_pid()
        if not quiet:
            print(f"{APP_NAME} is not running.")
        return 0
    for target in targets:
        _terminate(target)
    clear_pid()
    if not quiet:
        extra = len(targets) - 1
        note = f" (and {_plural(extra, 'stray panel')})" if extra > 0 else ""
        print(f"{APP_NAME} closed{note}.")
    return 0


def status_panel(args) -> int:
    pid = read_pid()
    state = load_state()
    if pid:
        print(f"{APP_NAME}: showing (pid {pid})")
    else:
        print(f"{APP_NAME}: not running")
    strays = stray_panels(exclude=pid)
    if strays:
        print(f"  {_plural(len(strays), 'unregistered panel')} also running "
              f"({', '.join(str(s) for s in strays)}) — --stop closes them too")
    print(f"  autostart with each session: "
          f"{'on' if state.get('autostart') else 'off'}")
    roots = discover_roots(args.root)
    if roots:
        for root in roots:
            count = sum(1 for _ in root.rglob("*.jsonl"))
            print(f"  watching {root}  ({count} transcripts)")
    else:
        print("  no transcript directory found — pass --root, "
              "or set CLAUDE_CONFIG_DIR")
    return 0


def install_app(args) -> int:
    if sys.platform != "darwin":
        print("--install-app builds a macOS .app bundle; on this platform "
              "start the panel with /tokens-watch or "
              "`python3 tokenwatch.py --start`.", file=sys.stderr)
        return 1
    target_dir = Path(os.path.expanduser(args.app_dir or "~/Applications"))
    bundle = target_dir / f"{APP_NAME}.app"
    macos = bundle / "Contents" / "MacOS"
    try:
        macos.mkdir(parents=True, exist_ok=True)
        launcher = macos / "tokenwatch"
        # The bundle starts the panel through the ordinary detached path
        # rather than becoming the panel itself. That path registers the
        # panel the instant it is spawned, so double-clicking the app while
        # one is already up cannot leave two panels on screen.
        preamble = ""
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        if configured:
            # A GUI launch inherits none of the shell's environment, so the
            # directory chosen at install time is baked in.
            preamble = f'export CLAUDE_CONFIG_DIR="{configured}"\n'
        launcher.write_text(
            "#!/bin/sh\n"
            + preamble
            + f'exec "{sys.executable}" '
            f'"{Path(__file__).resolve()}" --start "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        info = {
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": "dev.tokentracker.tokenwatch",
            "CFBundleExecutable": "tokenwatch",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.1.0",
            "CFBundleVersion": "1.1.0",
            "NSHighResolutionCapable": True,
            # Accessory app: a floating panel, so no Dock icon and no menu bar.
            "LSUIElement": True,
        }
        with open(bundle / "Contents" / "Info.plist", "wb") as handle:
            plistlib.dump(info, handle)
    except OSError as exc:
        print(f"could not write the app bundle: {exc}", file=sys.stderr)
        return 1
    print(f"Installed {bundle}")
    print("  Launch it from Spotlight as “Claude Token Watch”. It has no Dock "
          "icon by design — close the panel with the ✕ in its corner.")
    return 0


def set_autostart(enabled: bool) -> int:
    state = load_state()
    state["autostart"] = bool(enabled)
    save_state(state)
    print(f"autostart {'enabled' if enabled else 'disabled'} — the panel will "
          f"{'open with each new Claude session' if enabled else 'only open when you ask'}.")
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def build_watcher(args) -> Watcher:
    roots = discover_roots(args.root)
    if not roots:
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        looked = (f"{configured.rstrip('/')}/projects (from CLAUDE_CONFIG_DIR)"
                  if configured else "~/.claude/projects")
        raise SystemExit(
            f"No Claude transcript directory found. Looked in {looked}. "
            "Use Claude Code once to create it, or pass --root DIR."
        )
    pricing = Pricing(Path(os.path.expanduser(args.pricing)))
    return Watcher(
        roots, pricing,
        include_sidechains=not args.no_subagents,
        window_days=args.days,
    )


def run_panel(args) -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print(
            "Tkinter is not available for this Python, so the desktop panel "
            "cannot open.\n"
            "  macOS  : install Python from python.org, or `brew install "
            "python-tk`\n"
            "  Debian : sudo apt install python3-tk\n"
            "  Fedora : sudo dnf install python3-tkinter\n"
            "Meanwhile `python3 token_report.py` still reports from the "
            "terminal.", file=sys.stderr,
        )
        return 1

    registered = read_pid()
    if registered is not None and registered != os.getpid():
        print(f"{APP_NAME} is already showing (pid {registered}). "
              f"Close that one first, or run --stop.", file=sys.stderr)
        return 1

    # Claim the slot before the slow work — priming transcripts and bringing
    # up Tk can take a few seconds, and a /tokens-watch arriving in that gap
    # would otherwise see nothing registered and open a second panel.
    write_pid(os.getpid())
    try:
        watcher = build_watcher(args)
        watcher.prime()
        state = load_state()
        if args.scope:
            state["scope"] = args.scope
        panel = Panel(watcher, state, args)
    except BaseException:
        clear_pid(os.getpid())
        raise

    # Registered only now, and deliberately so: initialising Tk installs
    # Tcl's own signal handling, which replaces anything set up beforehand.
    # A stop arriving during those first moments falls to the default
    # disposition, which is harmless — there is no window state to save yet.
    def _bye(_signum, _frame):
        panel.quit()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _bye)
        except (OSError, ValueError):
            pass
    try:
        panel.run()
    finally:
        clear_pid(os.getpid())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tokenwatch",
        description="A small always-on-top desktop panel showing Claude "
                    "token usage as it happens.",
    )
    parser.add_argument(
        "verb", nargs="?",
        choices=["on", "off", "toggle", "status", "snapshot",
                 "auto-on", "auto-off", "install-app"],
        help="what to do (default: toggle). The flags below do the same "
             "things and exist for scripting.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--toggle", action="store_true",
                      help="show the panel, or close it if it is already up "
                           "(the default)")
    mode.add_argument("--start", action="store_true",
                      help="show the panel, detached from this terminal")
    mode.add_argument("--stop", action="store_true", help="close the panel")
    mode.add_argument("--run", action="store_true",
                      help="run the panel in the foreground (for debugging)")
    mode.add_argument("--status", action="store_true",
                      help="report whether the panel is up and what it watches")
    mode.add_argument("--snapshot", action="store_true",
                      help="print the current numbers as JSON and exit")
    mode.add_argument("--install-app", action="store_true",
                      help="macOS: install a dockless .app into ~/Applications")
    mode.add_argument("--autostart", action="store_true",
                      help="open the panel only if autostart is enabled "
                           "(used by the session hook)")
    mode.add_argument("--autostart-on", action="store_true",
                      help="open the panel automatically with each new session")
    mode.add_argument("--autostart-off", action="store_true",
                      help="stop opening the panel automatically")

    parser.add_argument("--scope", choices=SCOPES,
                        help="which total to headline (default: last used)")
    parser.add_argument("--interval", type=float, default=1.0, metavar="SECONDS",
                        help="how often to check for new turns (default 1.0)")
    parser.add_argument("--opacity", type=float, default=0.96, metavar="0-1",
                        help="panel opacity (default 0.96)")
    parser.add_argument("--decorated", action="store_true",
                        help="keep the normal window frame instead of the "
                             "borderless panel")
    parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS,
                        metavar="N",
                        help=f"days of history to hold in memory "
                             f"(default {DEFAULT_WINDOW_DAYS})")
    parser.add_argument("--no-subagents", action="store_true",
                        help="exclude subagent (sidechain) turns")
    parser.add_argument("--root", action="append", metavar="DIR",
                        help="transcript directory to watch (repeatable)")
    parser.add_argument("--pricing", metavar="PATH", default=str(DEFAULT_PRICING),
                        help="rates file (default scripts/pricing.json)")
    parser.add_argument("--debug", action="store_true",
                        help="narrate every tick into "
                             "~/.claude/tokenwatch/tokenwatch.log")
    parser.add_argument("--app-dir", metavar="DIR",
                        help="where --install-app puts the bundle "
                             "(default ~/Applications)")
    return parser


VERB_FLAGS = {
    "on": "start",
    "off": "stop",
    "toggle": "toggle",
    "status": "status",
    "snapshot": "snapshot",
    "auto-on": "autostart_on",
    "auto-off": "autostart_off",
    "install-app": "install_app",
}


def main(argv: list[str] | None = None) -> int:
    global DEBUG
    args = build_parser().parse_args(argv)
    DEBUG = DEBUG or bool(args.debug)

    # `tokenwatch on` and `tokenwatch --start` mean the same thing.
    if args.verb:
        setattr(args, VERB_FLAGS[args.verb], True)

    if args.run:
        return run_panel(args)
    if args.stop:
        return stop_panel()
    if args.status:
        return status_panel(args)
    if args.install_app:
        return install_app(args)
    if args.autostart_on:
        set_autostart(True)
        return start_panel(args)
    if args.autostart_off:
        stop_panel(quiet=True)
        return set_autostart(False)
    if args.autostart:
        if not load_state().get("autostart"):
            return 0
        return start_panel(args, quiet=True)
    if args.snapshot:
        watcher = build_watcher(args)
        watcher.prime()
        print(json.dumps(
            watcher.snapshot(args.scope or load_state().get("scope", "today")),
            indent=2, default=str,
        ))
        return 0
    if args.start:
        return start_panel(args)

    # Default: toggle.
    if read_pid():
        return stop_panel()
    return start_panel(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
