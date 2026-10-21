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
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from urllib.parse import quote as url_quote
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Surfaces, keyed by the transcript's `entrypoint` field.
# Named "<tool> · <where>", matching Copilot's "Copilot · VS Code", so the same
# editor used through two tools reads as two places.
SURFACE_LABELS = {
    "cli": "Claude Code · Terminal",
    "claude-code": "Claude Code · Terminal",
    "local-agent": "Cowork · Desktop",
    "vscode": "Claude Code · VS Code",
    "vscode-extension": "Claude Code · VS Code",
    "jetbrains": "Claude Code · JetBrains",
    "intellij": "Claude Code · JetBrains",
    "desktop": "Claude Code · Desktop app",
    "web": "Claude Code · Web",
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

def _config_home() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(os.path.expanduser(env))
    return Path(os.path.expanduser("~")) / ".claude"


# Lives outside the plugin, so it survives plugin updates.
USER_DIR = _config_home() / "token-tracker"
USER_PRICING = USER_DIR / "pricing.json"
DEFAULT_PRICING = USER_PRICING            # your own rates, optional
REMOTE_CACHE = USER_DIR / "pricing-remote.json"
# Where agent_tracer.py logs agents you build yourself; scanned automatically.
CUSTOM_AGENTS_DIR = USER_DIR / "agents"

# Live price sources, downloaded daily and only ever downloaded: nothing about
# the user or their usage is sent. Anthropic's own pricing page is the
# authority for Claude models; the LiteLLM list fills in everything else
# (other providers, and any Claude model the page has not reached yet).
# TOKEN_TRACKER_PRICING_URL replaces the second source with your own file in
# either format; TOKEN_TRACKER_OFFLINE=1 never touches the network.
ANTHROPIC_PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
# GitHub's billing docs state what one Copilot AI credit is worth in dollars.
GITHUB_CREDIT_URL = ("https://docs.github.com/api/article/body?pathname=/en/copilot/"
                     "concepts/billing/usage-based-billing-for-individuals")
GITHUB_CREDIT_PAGE = ("https://docs.github.com/en/copilot/concepts/billing/"
                      "usage-based-billing-for-individuals")
DEFAULT_REMOTE_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
                      "model_prices_and_context_window.json")
REMOTE_MAX_AGE = 24 * 3600      # refetch once a day
REMOTE_RETRY = 3600             # after a miss or failure, at most hourly
REMOTE_TIMEOUT = 4.0
REMOTE_MAX_BYTES = 16 * 1024 * 1024

# claude-opus-5-5, claude-haiku-4-5, claude-opus-4 ...
_ID_NEW = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?$")
# claude-3-5-sonnet, claude-3-opus ...
_ID_OLD = re.compile(r"^claude-(\d+)(?:-(\d{1,2}))?-([a-z]+)$")


def _family_version(key: str) -> tuple[str, tuple[int, int]] | None:
    """claude-opus-5-5 -> ("opus", (5, 5)). None for anything else."""
    m = _ID_NEW.match(key)
    if m:
        return m.group(1), (int(m.group(2)), int(m.group(3) or 0))
    m = _ID_OLD.match(key)
    if m:
        return m.group(3), (int(m.group(1)), int(m.group(2) or 0))
    return None


def _derive_label(key: str, fallback: str) -> str:
    """claude-opus-5-5 -> "Opus 5.5", so a model nobody has listed yet still
    reads properly instead of being folded into an older one."""
    fv = _family_version(key)
    if not fv:
        return fallback or "unknown"
    family, (major, minor) = fv
    return f"{family.title()} {major}" + (f".{minor}" if minor else "")


def _per_mtok(value) -> float | None:
    try:
        return float(value) * 1e6
    except (TypeError, ValueError):
        return None


def _parse_price_list(raw) -> dict[str, dict]:
    """Accept this plugin's own format ({"models": {...}}, USD per MTok) or the
    LiteLLM format (one entry per model, USD per token). Unknown cache rates
    fall back to Anthropic's published ratios to input."""
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("models"), dict):
        return {k.lower(): v for k, v in raw["models"].items()
                if isinstance(v, dict) and "input" in v and "output" in v}
    out: dict[str, dict] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        inp = _per_mtok(entry.get("input_cost_per_token"))
        outp = _per_mtok(entry.get("output_cost_per_token"))
        if inp is None or outp is None:
            continue
        w5 = _per_mtok(entry.get("cache_creation_input_token_cost"))
        w1 = _per_mtok(entry.get("cache_creation_input_token_cost_above_1hr"))
        cr = _per_mtok(entry.get("cache_read_input_token_cost"))
        out[name.lower()] = {
            "input": inp,
            "output": outp,
            "cache_write_5m": inp * 1.25 if w5 is None else w5,
            "cache_write_1h": inp * 2.0 if w1 is None else w1,
            "cache_read": inp * 0.1 if cr is None else cr,
        }
    return out


_MONEY = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _md_tables(text: str):
    """Yield (section heading, header cells, body rows) for each Markdown table."""
    heading, lines = "", text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
        if (line.lstrip().startswith("|") and i + 1 < len(lines)
                and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1])):
            header = [c.lower() for c in _cells(line)]
            rows, i = [], i + 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            yield heading, header, rows
            continue
        i += 1


def _claude_ids(name: str) -> list[str]:
    """ "Claude Opus 5 / Claude Opus 4.8" -> [claude-opus-5, claude-opus-4-8].
    Links, footnotes and parenthetical notes are dropped."""
    name = re.sub(r"<[^>]+>", "", name)
    name = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", name)
    name = re.sub(r"\([^)]*\)", "", name)
    ids = []
    for part in name.split("/"):
        m = re.match(r"^\s*claude\s+([a-z]+)\s+(\d+)(?:\.(\d+))?\s*$",
                     part, re.I)
        if m:
            ids.append("claude-" + m.group(1).lower() + "-" + m.group(2)
                       + (f"-{m.group(3)}" if m.group(3) else ""))
    return ids


def _money(cell: str) -> float | None:
    m = _MONEY.search(cell or "")
    return float(m.group(1)) if m else None


def _sections(text: str) -> dict[str, str]:
    """Markdown heading (lower case) -> the text under it."""
    out: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("#"):
            current = line.lstrip("#").strip().lower()
            out.setdefault(current, [])
        else:
            out.setdefault(current, []).append(line)
    return {k: "\n".join(v) for k, v in out.items()}


_CACHE_FIELDS = ("cache_write_5m", "cache_write_1h", "cache_read")
_RATE_FIELDS = ("input", "output") + _CACHE_FIELDS


def _scaled(base: dict, inp: float, outp: float) -> dict:
    """A rate card with new input/output prices. The page says caching
    multipliers stack on top of fast-mode and batch prices, so cache rates
    scale by the same factor as input."""
    factor = inp / base["input"] if base.get("input") else 0.0
    out = {"input": inp, "output": outp}
    for field in _CACHE_FIELDS:
        out[field] = base[field] * factor
    return out


def _parse_anthropic_page(text: str) -> dict:
    """Read every price this tool needs off Anthropic's pricing page.

    Columns are found by their headings, not position, so a reordered or
    widened table still parses. Returns, all USD per MTok:
      models  {id: rates}, with "long": {"over": N, ...rates} where a model
              charges more for prompts over N tokens (Haiku 5.5)
      fast    {id: rates} for fast mode
      batch   {id: rates} for the Batch API
      geo_us  multiplier for US-only inference, or None
      web_search_per_1k  USD per 1,000 web searches, or None
    """
    models: dict[str, dict] = {}
    fast: dict[str, tuple[float, float]] = {}
    batch: dict[str, tuple[float, float]] = {}

    def col(header, *words):
        for idx, cell in enumerate(header):
            if all(w in cell for w in words):
                return idx
        return None

    def pair(row, i_in, i_out):
        inp = _money(row[i_in]) if i_in is not None and i_in < len(row) else None
        outp = _money(row[i_out]) if i_out is not None and i_out < len(row) else None
        return (inp, outp) if inp is not None and outp is not None else None

    for heading, header, rows in _md_tables(text):
        if not header or "model" not in header[0]:
            continue
        if "model pricing" in heading:
            cols = {
                "input": col(header, "base input"),
                "cache_write_5m": col(header, "5m"),
                "cache_write_1h": col(header, "1h"),
                "cache_read": col(header, "cache hit"),
                "output": col(header, "output"),
            }
            for row in rows:
                rates = {f: _money(row[c]) if c is not None and c < len(row)
                         else None for f, c in cols.items()}
                if any(rates[f] is None for f in _RATE_FIELDS):
                    continue                     # never fill a gap with a guess
                over = re.search(r"over\s+([\d,]+)\s+tokens", row[0], re.I)
                for model_id in _claude_ids(row[0]):
                    if over:
                        models.setdefault(model_id, {})["long"] = {
                            "over": int(over.group(1).replace(",", "")), **rates}
                    else:
                        entry = models.setdefault(model_id, {})
                        if "input" not in entry:
                            entry.update(rates)
        elif "fast mode" in heading or "batch" in heading:
            target = fast if "fast mode" in heading else batch
            i_in, i_out = col(header, "input"), col(header, "output")
            for row in rows:
                got = pair(row, i_in, i_out)
                if got:
                    for model_id in _claude_ids(row[0]):
                        target.setdefault(model_id, got)

    models = {k: v for k, v in models.items() if "input" in v}
    sections = _sections(text)

    geo_us = None
    m = re.search(r"inference_geo.{0,200}?(\d+(?:\.\d+)?)x multiplier",
                  sections.get("data residency pricing", ""), re.S)
    if m:
        geo_us = float(m.group(1))

    web = None
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)\s+per\s+1,000\s+searches",
                  sections.get("web search tool", ""), re.I)
    if m:
        web = float(m.group(1))

    return {
        "models": models,
        "fast": {k: _scaled(models[k], *v) for k, v in fast.items() if k in models},
        "batch": {k: _scaled(models[k], *v) for k, v in batch.items() if k in models},
        "geo_us": geo_us,
        "web_search_per_1k": web,
    }


def _parse_github_credit(text: str) -> dict:
    """ "1 AI credit = $0.01 USD" off GitHub's Copilot billing docs."""
    m = re.search(r"1\s+AI\s+credit\s*=\s*\$\s*([0-9]+(?:\.[0-9]+)?)\s*USD",
                  text, re.I)
    return {"usd_per_credit": float(m.group(1))} if m else {}


def _read_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _download(url: str) -> bytes:
    """Fetch `url` with the standard library, falling back to the system curl.
    The python.org build on macOS ships without root certificates until
    "Install Certificates.command" is run, so urllib alone fails TLS there."""
    req = urllib.request.Request(url, headers={"User-Agent": "claude-token-tracker"})
    try:
        with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT) as resp:
            return resp.read(REMOTE_MAX_BYTES + 1)
    except urllib.error.URLError as exc:
        if not isinstance(getattr(exc, "reason", None), ssl.SSLError):
            raise
        curl = shutil.which("curl")
        if not curl or not url.startswith("https://"):
            raise
        done = subprocess.run(
            [curl, "-fsSL", "--max-time", str(int(REMOTE_TIMEOUT * 2)),
             "--max-filesize", str(REMOTE_MAX_BYTES), url],
            capture_output=True, timeout=REMOTE_TIMEOUT * 3, check=False)
        if done.returncode != 0:
            raise
        return done.stdout


class Pricing:
    """Per-million-token rates, never hard-coded and never guessed.

    A rate comes from one of three places, first hit wins:
      1. your own file    ~/.claude/token-tracker/pricing.json
      2. Anthropic        the official pricing page, fetched daily
      3. LiteLLM          a public multi-provider price list, fetched daily
    Copilot records its own charge in AI credits; GitHub's billing docs give
    the dollar value of one credit, also fetched daily.
    Both live sources are cached in ~/.claude/token-tracker/, so a day
    offline still prices at the last rates fetched. When a turn needs any
    rate none of these has (the model, fast mode, batch, US-only inference,
    web search), the turn is left unpriced and shown without a cost.
    """

    def __init__(self, path: Path | None = USER_PRICING,
                 remote: bool | None = None):
        user = _read_json(path) if path else None
        self.overrides = _parse_price_list(user) if user else {}
        mods = (user or {}).get("modifiers") or {}
        self.user_fast = {k.lower(): v for k, v in
                          (mods.get("fast_mode") or {}).items() if isinstance(v, dict)}
        self.user_geo_us = mods.get("inference_geo_us_multiplier")
        self.user_batch = mods.get("batch_multiplier")
        self.user_web = (user or {}).get("web_search_per_1k_requests")
        self.user_credit = (user or {}).get("copilot_usd_per_credit")

        if remote is None:
            remote = os.environ.get("TOKEN_TRACKER_OFFLINE", "") in ("", "0")
        self.remote_url = os.environ.get("TOKEN_TRACKER_PRICING_URL",
                                         DEFAULT_REMOTE_URL)
        self.remote_enabled = bool(remote)
        self.official: dict = {}                 # parsed Anthropic page
        self.remote: dict[str, dict] = {}        # LiteLLM (or your own URL)
        self.github: dict = {}                   # Copilot AI credit -> USD
        self.remote_fetched = 0.0
        self.remote_error = ""
        self._last_attempt = 0.0
        self._refreshing = False
        self._lock = threading.Lock()

        self.unknown_models: set[str] = set()
        self._resolved: dict[tuple[str, str], dict | None] = {}

        self._load_remote_cache()
        if self.remote_enabled and self._remote_stale():
            self.refresh_remote()                # once a day, a few seconds max

    # -- live price lists -------------------------------------------------- #

    def _load_remote_cache(self) -> None:
        cached = _read_json(REMOTE_CACHE)
        if not isinstance(cached, dict):
            return
        official = cached.get("anthropic")
        if isinstance(official, dict) and isinstance(official.get("models"), dict):
            self.official = official
        other = cached.get("litellm") or {}
        self.remote = other.get("models") or {}
        github = cached.get("github")
        if isinstance(github, dict) and github.get("usd_per_credit") is not None:
            self.github = github
        self.remote_fetched = float(cached.get("_fetched") or 0)

    def _remote_stale(self) -> bool:
        age = time.time() - self.remote_fetched
        return (age > REMOTE_MAX_AGE or not self.official.get("models")
                or (not self.github and age > REMOTE_RETRY))

    @property
    def has_rates(self) -> bool:
        return bool(self.overrides or self.official.get("models") or self.remote)

    @property
    def live_checked(self) -> str:
        """When the live prices were last fetched, for display."""
        if not self.remote_fetched:
            return "never"
        return dt.datetime.fromtimestamp(self.remote_fetched).strftime(
            "%Y-%m-%d %H:%M")

    def refresh_remote(self) -> bool:
        """Download both price lists now, in parallel. Never raises. A source
        that fails keeps its last cached rates; False only if both failed."""
        self._last_attempt = time.time()
        results: dict[str, object] = {}

        def fetch(name: str, url: str, parse) -> None:
            try:
                if not url.startswith(("https://", "file://")):
                    raise ValueError(f"refusing non-https URL {url}")
                body = _download(url)
                if len(body) > REMOTE_MAX_BYTES:
                    raise ValueError("price list too large")
                parsed = parse(body)
                if not parsed.get("models") and not parsed.get("usd_per_credit"):
                    raise ValueError("no prices found")
                results[name] = parsed
            except Exception as exc:  # network, TLS, format: all non-fatal
                results[name] = exc

        jobs = [
            ("anthropic", ANTHROPIC_PRICING_URL,
             lambda b: _parse_anthropic_page(b.decode("utf-8", "replace"))),
            ("litellm", self.remote_url,
             lambda b: {"models": _parse_price_list(json.loads(b))}),
            ("github", GITHUB_CREDIT_URL,
             lambda b: _parse_github_credit(b.decode("utf-8", "replace"))),
        ]
        threads = [threading.Thread(target=fetch, args=job, daemon=True)
                   for job in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(REMOTE_TIMEOUT * 4)

        errors = [f"{n}: {type(r).__name__}: {r}" for n, r in results.items()
                  if isinstance(r, Exception)]
        errors += [f"{n}: timed out" for n, _, _ in jobs if n not in results]
        good = {n: r for n, r in results.items() if isinstance(r, dict)}
        with self._lock:
            self.remote_error = "; ".join(errors)
            if not good:
                return False
            if "anthropic" in good:
                self.official = {"source": ANTHROPIC_PRICING_URL,
                                 **good["anthropic"]}
            if "litellm" in good:
                self.remote = good["litellm"]["models"]
            if "github" in good:
                self.github = {"source": GITHUB_CREDIT_PAGE, **good["github"]}
            self.remote_fetched = time.time()
            # Models that were unpriced may resolve now.
            self._resolved.clear()
            self.unknown_models.clear()
            snapshot = {
                "_fetched": self.remote_fetched,
                "anthropic": self.official,
                "litellm": {"source": self.remote_url, "models": self.remote},
                "github": self.github,
            }
        try:
            USER_DIR.mkdir(parents=True, exist_ok=True)
            tmp = REMOTE_CACHE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh)
            os.replace(tmp, REMOTE_CACHE)
        except OSError:
            pass                                 # still use it in memory
        return True

    def _refresh_in_background(self) -> None:
        """For the long-running panel: pick up a model released after it
        started, without blocking the UI thread on the network."""
        if not self.remote_enabled or self._refreshing:
            return
        if time.time() - self._last_attempt < REMOTE_RETRY:
            return
        self._refreshing = True
        self._last_attempt = time.time()

        def run():
            try:
                self.refresh_remote()
            finally:
                self._refreshing = False

        threading.Thread(target=run, daemon=True).start()

    # -- lookup ------------------------------------------------------------ #

    @staticmethod
    def _normalise(model_id: str) -> str:
        """Reduce any spelling of a model id to its bare first-party form:
        claude-haiku-4-5-20251001, claude-opus-4-5@20251101,
        us.anthropic.claude-opus-5-5-v1:0, vertex_ai/claude-opus-5-5,
        claude-opus-4-6[1m]  ->  claude-haiku-4-5, claude-opus-4-5, ...
        """
        key = (model_id or "").strip().lower()
        key = key.split("[", 1)[0].split("@", 1)[0]
        key = key.rsplit("/", 1)[-1]
        key = re.sub(r"^(?:[a-z0-9-]+\.)?anthropic\.", "", key)
        key = re.sub(r"-v\d+(?::\d+)?$", "", key)
        key = re.sub(r"(?<=\d)\.(?=\d)", "-", key)     # claude-sonnet-4.5
        parts = key.split("-")
        while parts and parts[-1].isdigit() and len(parts[-1]) == 8:
            parts.pop()
        return "-".join(parts)

    def _lookup(self, table: dict, model_id: str):
        raw = (model_id or "").strip().lower()
        for candidate in (raw, self._normalise(model_id)):
            if candidate in table:
                return table[candidate], candidate
        return None, ""

    def _base(self, model_id: str) -> tuple[dict | None, str, str]:
        """(rates, matched key, which source). Exact matches only, so
        claude-opus-5-5 is never mistaken for claude-opus-5."""
        for source, table in (("override", self.overrides),
                              ("anthropic", self.official.get("models") or {}),
                              ("litellm", self.remote)):
            rates, key = self._lookup(table, model_id)
            if rates is not None:
                return rates, key, source
        return None, "", ""

    def _variant(self, model_id: str, tier: str) -> dict | None:
        base, key, source = self._base(model_id)
        if base is None or tier == "standard":
            return base
        if tier == "fast":
            hit = self.user_fast.get(key)
            if hit:
                merged = dict(base)
                merged.update(hit)
                return merged
            hit, _ = self._lookup(self.official.get("fast") or {}, model_id)
            return hit
        if tier == "batch":
            if source == "override" and self.user_batch is not None:
                return {f: float(base[f]) * float(self.user_batch)
                        for f in _RATE_FIELDS}
            hit, _ = self._lookup(self.official.get("batch") or {}, model_id)
            return hit
        return None

    def rates(self, model_id: str, speed: str = "standard") -> dict | None:
        """Rates for a model at a tier: "standard", "fast" or "batch".
        None when no source publishes that rate."""
        with self._lock:
            known = (model_id, speed) in self._resolved
            hit = self._resolved.get((model_id, speed))
        if not known:
            hit = self._variant(model_id, speed)
            with self._lock:
                self._resolved[(model_id, speed)] = hit
        if hit is None and model_id and not model_id.startswith("<"):
            self.unknown_models.add(model_id if speed == "standard"
                                    else f"{model_id} ({speed})")
        if self.remote_enabled and (self._remote_stale() or hit is None):
            self._refresh_in_background()
        return hit

    def long_context(self, model_id: str) -> dict | None:
        """{"over": N, ...rates} when the model charges more past N prompt
        tokens, else None."""
        base, _, _ = self._base(model_id)
        return (base or {}).get("long")

    @property
    def geo_us_multiplier(self) -> float | None:
        if self.user_geo_us is not None:
            return float(self.user_geo_us)
        return self.official.get("geo_us")

    @property
    def copilot_usd_per_credit(self) -> float | None:
        """Dollars per Copilot AI credit, from GitHub's billing docs."""
        if self.user_credit is not None:
            return float(self.user_credit)
        return self.github.get("usd_per_credit")

    @property
    def web_search_per_1k(self) -> float | None:
        if self.user_web is not None:
            return float(self.user_web)
        return self.official.get("web_search_per_1k")

    def label(self, model_id: str) -> str:
        key = self._normalise(model_id)
        entry = self.overrides.get(key)
        if entry and entry.get("label"):
            return entry["label"]
        return _derive_label(key, model_id)


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
        # which tool made the call ("Claude Code", "Cowork", "Copilot", ...),
        # Copilot's charge in GitHub AI credits (its cost is that in dollars),
        # and its mode / chat location
        "via", "credits", "mode", "request_id",
        # who did the work and inside which loop: the agent ("main", or a
        # subagent's type such as "Explore"), that agent's id, the loop (one
        # prompt you typed and every model call it set off, subagents
        # included), how deep the subagent was spawned, the tools the call
        # used, model calls made (Copilot packs several into one request),
        # and Copilot's breakdown of the prompt by part
        "agent", "agent_id", "loop_id", "spawn_depth", "tools", "steps",
        "prompt_parts",
        # where to pick the conversation up again: its folder, its title
        # (Claude Code's own, or the name you gave it), and whether you named it
        "cwd", "title", "named",
    )

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))

    @property
    def agent_label(self) -> str:
        """ "Claude Code · main", "Claude Code · Explore", "Copilot · agent"."""
        return f"{self.via or 'unknown'} · {self.agent or 'main'}"

    @property
    def prompt_size(self) -> int:
        """Everything the model read on this call: fresh input plus cache."""
        return (self.input or 0) + (self.cache_read or 0) + \
               (self.cache_write_5m or 0) + (self.cache_write_1h or 0)

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
        CUSTOM_AGENTS_DIR,
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


def price_turn(pricing: Pricing, model_id: str, usage: dict, tok_in: int,
               tok_out: int, cache_read: int, write_5m: int, write_1h: int,
               searches: int) -> float | None:
    """Tokens x live rates. None when any rate this turn needs is unknown:
    a partial figure presented as a cost would be wrong, so none is shown."""
    if not (tok_in or tok_out or cache_read or write_5m or write_1h or searches):
        return 0.0                  # e.g. Claude Code's <synthetic> placeholders
    tier = "standard"
    if usage.get("speed") == "fast":
        tier = "fast"
    elif usage.get("service_tier") == "batch":
        tier = "batch"
    rates = pricing.rates(model_id, tier)
    if not rates:
        return None
    long = pricing.long_context(model_id)
    if long and tier == "standard" and \
            tok_in + cache_read + write_5m + write_1h > long["over"]:
        rates = long
    try:
        cost = (
            tok_in / 1e6 * float(rates["input"])
            + cache_read / 1e6 * float(rates["cache_read"])
            + write_5m / 1e6 * float(rates["cache_write_5m"])
            + write_1h / 1e6 * float(rates["cache_write_1h"])
            + tok_out / 1e6 * float(rates["output"])
        )
    except (KeyError, TypeError, ValueError):
        return None
    if usage.get("inference_geo") == "us":
        mult = pricing.geo_us_multiplier
        if mult is None:
            return None
        cost *= mult
    if searches:
        per_1k = pricing.web_search_per_1k
        if per_1k is None:
            return None
        cost += searches / 1000.0 * per_1k
    return cost


def _via(entrypoint: str | None) -> str:
    entry = (entrypoint or "").lower()
    if entry == "custom-agent":
        return "Custom agent"
    if entry == "local-agent":
        return "Cowork"
    if entry.startswith("sdk"):
        return "Agent SDK"
    return "Claude Code"


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
    tok_in = int(usage.get("input_tokens") or 0)
    tok_out = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)

    cost = price_turn(pricing, model_id, usage, tok_in, tok_out, cache_read,
                      write_5m, write_1h, searches)
    priced = cost is not None

    return Turn(
        ts=stamp,
        day=stamp.date().isoformat() if stamp else "(undated)",
        model=model_id,
        model_label=pricing.label(model_id),
        session=row.get("sessionId") or fallback_session,
        surface=(f"Custom agent · {row.get('appName') or 'unnamed'}"
                 if row.get("entrypoint") == "custom-agent"
                 else _surface_label(row.get("entrypoint"))),
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
        cost=cost or 0.0,
        priced=priced,
        via=_via(row.get("entrypoint")),
        credits=None,
        mode="",
        raw_total=tok_in + tok_out + cache_read + write_5m + write_1h,
    )


def _tool_names(message: dict) -> list[str]:
    """Names of the tools a model call used (names only, never inputs)."""
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [str(b.get("name")) for b in content
            if isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use")
            and b.get("name")]


def _mcp_server(tools: list[str]) -> str:
    """mcp__github__create_issue -> github."""
    for name in tools:
        if name.startswith("mcp__"):
            parts = name.split("__")
            if len(parts) >= 3 and parts[1]:
                return parts[1]
    return ""


class Linker:
    """Ties each model call to the loop it belongs to.

    A loop starts with a prompt you typed: Claude Code gives it a `promptId`,
    and every tool result in that loop carries the same id. Model calls hang
    off those rows through `parentUuid`, so following the chain places each
    call in its loop. A subagent's log sits in <session>/subagents/, beside a
    .meta.json naming its type and the tool call that spawned it, which puts
    the subagent's calls in the loop of the prompt that started it.

    Only ids, names and content-block types are read, never message text.
    """

    def __init__(self) -> None:
        self.loop_of: dict[str, str] = {}      # row uuid -> loop id
        self.tool_loop: dict[str, str] = {}    # tool_use id -> loop id
        self.metas: dict[Path, dict] = {}
        self.by_key: dict[str, Turn] = {}      # dedupe key -> Turn
        self.titles: dict[str, dict] = {}      # session -> {"ai":, "custom":}

    def meta(self, path: Path) -> dict:
        if path.parent.name != "subagents":
            return {}
        if path not in self.metas:
            data = _read_json(path.with_name(path.stem + ".meta.json"))
            self.metas[path] = data if isinstance(data, dict) else {}
        return self.metas[path]

    def observe(self, row: dict, path: Path) -> str:
        """Record this row's place in its loop; return the loop id."""
        kind = row.get("type")
        if kind in ("ai-title", "custom-title") and row.get("sessionId"):
            entry = self.titles.setdefault(row["sessionId"], {})
            value = row.get("aiTitle") if kind == "ai-title" else row.get("customTitle")
            if value:
                entry["ai" if kind == "ai-title" else "custom"] = str(value)[:200]
            return ""
        meta = self.meta(path)
        loop = ""
        if meta.get("toolUseId"):
            loop = self.tool_loop.get(meta["toolUseId"], "")
        if not loop:
            loop = (row.get("promptId")
                    or self.loop_of.get(row.get("parentUuid") or "", ""))
        if not loop and meta:
            loop = "agent:" + str(row.get("agentId") or path.stem)
        if row.get("uuid") and loop:
            self.loop_of[row["uuid"]] = loop
        message = row.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use" \
                        and block.get("id") and loop:
                    self.tool_loop[block["id"]] = loop
        return loop

    def merge(self, key: str, row: dict) -> None:
        """A later row of a call already counted: Claude Code writes one row
        per content block with the same usage, so only its tools are new."""
        turn = self.by_key.get(key)
        message = row.get("message")
        if turn is None or not isinstance(message, dict):
            return
        extra = _tool_names(message)
        if extra:
            turn.tools = (turn.tools or []) + extra
            if not turn.mcp:
                turn.mcp = _mcp_server(turn.tools)

    def annotate(self, turn: Turn, row: dict, path: Path, loop: str,
                 key: str | None) -> Turn:
        meta = self.meta(path)
        turn.loop_id = loop
        turn.agent_id = str(row.get("agentId") or "")
        turn.agent = str(row.get("attributionAgent") or meta.get("agentType")
                         or ("subagent" if turn.is_sidechain else "main"))
        turn.spawn_depth = int(meta.get("spawnDepth") or row.get("spawnDepth") or 0)
        turn.tools = _tool_names(row.get("message") or {})
        if not turn.mcp:
            turn.mcp = _mcp_server(turn.tools)
        turn.steps = 1
        turn.cwd = str(row.get("cwd") or "")
        if key:
            self.by_key[key] = turn
        return turn

    def apply_titles(self, turns: list[Turn]) -> None:
        """Titles are written as their own rows, often after the turns, so
        they are attached once everything has been read."""
        for turn in turns:
            entry = self.titles.get(turn.session or "")
            if entry and turn.via != "Copilot":
                turn.title = entry.get("custom") or entry.get("ai") or ""
                turn.named = bool(entry.get("custom"))


def collect_turns(roots: list[Path], pricing: Pricing, use_utc: bool,
                  include_sidechains: bool = True,
                  include_copilot: bool = True) -> tuple[list[Turn], dict]:
    """Read every transcript under `roots`, plus Copilot Chat's local logs,
    and return priced turns + stats."""
    seen_requests: set[str] = set()
    turns: list[Turn] = []
    stats = {"files": 0, "lines": 0, "bad_lines": 0, "duplicates": 0}

    files: list[Path] = []
    for root in roots:
        files.extend(sorted(root.rglob("*.jsonl")))

    linker = Linker()
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

                loop = linker.observe(row, path)
                usage = row_usage(row)
                if usage is None:
                    continue

                dedupe_key = row_dedupe_key(row)
                if dedupe_key:
                    if dedupe_key in seen_requests:
                        stats["duplicates"] += 1
                        linker.merge(dedupe_key, row)
                        continue
                    seen_requests.add(dedupe_key)

                if row.get("isSidechain") and not include_sidechains:
                    continue

                turns.append(linker.annotate(turn_from_row(
                    row, usage, pricing, use_utc, fallback_session=path.stem
                ), row, path, loop, dedupe_key))

    linker.apply_titles(turns)
    stats["copilot_files"] = 0
    if include_copilot:
        import copilot_usage                  # imports this module; load late
        copilot, stats["copilot_files"] = copilot_usage.collect(
            pricing, use_utc, include_sidechains)
        turns.extend(copilot)
    return turns, stats


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #

BUCKET_FIELDS = {
    "day": "day",
    "model": "model_label",
    "app": "via",
    "tool": "via",              # older name for "app"
    "agent": "agent_label",
    "surface": "surface",
    "mode": "mode",
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
        "copilot_turns": 0, "credits": 0.0, "credit_turns": 0,
    }


def add_turn(bucket: dict, turn: Turn) -> None:
    """Fold one turn into a bucket. Every cost is in dollars; Copilot's AI
    credits are also kept, to show the conversion."""
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
    if turn.via == "Copilot":
        bucket["copilot_turns"] += 1
        if turn.credits is not None:
            bucket["credits"] += turn.credits
            bucket["credit_turns"] += 1
    if not turn.priced:
        bucket["unpriced"] += 1


def aggregate(turns: list[Turn], field: str) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(blank_bucket)
    for turn in turns:
        add_turn(out[getattr(turn, field) or "(none)"], turn)
    return dict(out)


def totals(turns: list[Turn]) -> dict:
    agg = blank_bucket()
    for turn in turns:
        add_turn(agg, turn)
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
        if turn.via == "Copilot" or not turn.cache_read:
            continue
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


def fmt_money(value: float | None) -> str:
    if value is None:
        return NO_PRICE
    if value and abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


NO_PRICE = "—"


def fmt_credits(value: float) -> str:
    return f"{value:,.2f} AI credits"


def dollar_turns(bucket: dict) -> int:
    """Turns with a known dollar cost."""
    return bucket["turns"] - bucket["unpriced"]


def fmt_cost(bucket: dict) -> str:
    """A bucket's cost in US dollars, or a dash when none of its turns could
    be priced. A trailing + marks a total that leaves out unpriced turns."""
    if dollar_turns(bucket) <= 0:
        return NO_PRICE
    return fmt_money(bucket["cost"]) + ("+" if bucket["unpriced"] else "")


def cost_by_tool(turns: list[Turn]) -> list[tuple[str, dict]]:
    """(tool, bucket) for each tool, heaviest cost first."""
    tools = aggregate(turns, "via")
    return sorted(tools.items(), key=lambda kv: (-kv[1]["cost"], kv[0]))


def tool_split(turns: list[Turn]) -> str:
    """ "Claude Code $20.44 · Copilot $1.40": the platform tag on a total."""
    return " · ".join(f"{name} {fmt_cost(b)}" for name, b in cost_by_tool(turns))


def bucket_cost(bucket: dict) -> float | None:
    """For JSON: null rather than a misleading 0 when nothing was priced."""
    if dollar_turns(bucket) <= 0:
        return None
    return round(bucket["cost"], 6)


def bucket_credits(bucket: dict) -> float | None:
    if not bucket.get("credit_turns"):
        return None
    return round(bucket["credits"], 6)


# --------------------------------------------------------------------------- #
# agents, loops, tools, harness
# --------------------------------------------------------------------------- #

def loop_stats(turns: list[Turn]) -> list[dict]:
    """One row per agent loop: a prompt you sent and every model call it set
    off, subagents included. Heaviest first."""
    loops: dict[str, dict] = {}
    for turn in turns:
        if not turn.loop_id:
            continue
        loop = loops.get(turn.loop_id)
        if loop is None:
            loop = loops[turn.loop_id] = {
                "loop": turn.loop_id, "bucket": blank_bucket(), "calls": 0,
                "tool_calls": 0, "subagents": set(), "start": turn.ts,
                "end": turn.ts, "app": turn.via, "project": turn.project,
                "session": turn.session, "models": set(),
            }
        add_turn(loop["bucket"], turn)
        loop["calls"] += turn.steps or 1
        loop["tool_calls"] += len(turn.tools or [])
        if turn.is_sidechain and turn.agent_id:
            loop["subagents"].add(turn.agent_id)
        if not turn.is_sidechain:
            loop["app"] = turn.via
        loop["models"].add(turn.model_label)
        if turn.ts and (loop["start"] is None or turn.ts < loop["start"]):
            loop["start"] = turn.ts
        if turn.ts and (loop["end"] is None or turn.ts > loop["end"]):
            loop["end"] = turn.ts
    return sorted(loops.values(), key=lambda l: -l["bucket"]["raw_total"])


def tool_stats(turns: list[Turn]) -> dict[str, dict]:
    """Per tool: how often it was called, and the model calls that used it.
    One call can use several tools, so the token figures overlap."""
    out: dict[str, dict] = {}
    for turn in turns:
        names = turn.tools or []
        for name in set(names):
            label = name
            if name.startswith("mcp__"):
                parts = name.split("__", 2)
                if len(parts) == 3:
                    label = f"MCP {parts[1]} · {parts[2]}"
            entry = out.get(label)
            if entry is None:
                entry = out[label] = {"calls": 0, "bucket": blank_bucket(),
                                      "apps": set()}
            entry["calls"] += names.count(name)
            add_turn(entry["bucket"], turn)
            entry["apps"].add(turn.via)
    return out


def harness_stats(turns: list[Turn]) -> dict:
    """What the harness itself costs: the system prompt, tool definitions and
    memory sent before any work starts, and the history re-sent every call.

    For Claude Code-style logs, a session's first model call reads little
    besides the harness and your first prompt, so its prompt size stands in
    for the harness size. Copilot records the prompt's parts directly."""
    first: dict[str, Turn] = {}
    for turn in turns:
        if turn.via == "Copilot" or turn.is_sidechain or not turn.session:
            continue
        seen = first.get(turn.session)
        if seen is None or (turn.ts and seen.ts and turn.ts < seen.ts):
            first[turn.session] = turn
    setup = sorted(t.prompt_size for t in first.values() if t.prompt_size)
    claude = [t for t in turns if t.via != "Copilot"]
    read = sum(t.prompt_size for t in claude)
    cached = sum(t.cache_read or 0 for t in claude)
    parts: dict[str, float] = defaultdict(float)
    copilot_prompt = 0
    for turn in turns:
        if turn.prompt_parts:
            copilot_prompt += turn.input or 0
            for label, tokens in turn.prompt_parts.items():
                parts[label] += tokens
    return {
        "sessions": len(setup),
        "setup_avg": sum(setup) / len(setup) if setup else 0,
        "setup_median": setup[len(setup) // 2] if setup else 0,
        "setup_max": setup[-1] if setup else 0,
        "prompt_read": read,
        "history_reread": cached,
        "history_share": cached / read if read else 0.0,
        "copilot_parts": dict(sorted(parts.items(), key=lambda kv: -kv[1])),
        "copilot_prompt": copilot_prompt,
    }


def agents_html(turns: list[Turn]) -> str:
    agents = aggregate(turns, "agent_label")
    runs: dict[str, set] = defaultdict(set)
    for turn in turns:
        if turn.is_sidechain and turn.agent_id:
            runs[turn.agent_label].add(turn.agent_id)
    rows = [[esc(name), fmt_int(len(runs.get(name, ()))) if name in runs else "",
             fmt_int(b["turns"]), fmt_tokens(b["raw_total"]), fmt_tokens(b["output"]),
             fmt_cost(b)]
            for name, b in sorted(agents.items(), key=lambda kv: -kv[1]["raw_total"])]
    return ("<h2>Agents and subagents</h2>"
            '<p class="sub">The main agent of each app, every subagent type it '
            "spawned (with how many times), and Copilot's modes. Subagent work is "
            "also counted in the loop that started it.</p>"
            + table(["Agent", "Runs", "Model calls", "Tokens", "Output", "Cost"],
                    rows))


def loops_html(turns: list[Turn]) -> str:
    loops = loop_stats(turns)
    if not loops:
        return ""
    calls = sorted(l["calls"] for l in loops)
    tools = sum(l["tool_calls"] for l in loops)
    with_sub = sum(1 for l in loops if l["subagents"])
    rows = []
    for l in loops[:15]:
        span = ""
        if l["start"] and l["end"]:
            span = human_span((l["end"] - l["start"]).total_seconds())
        rows.append([
            esc(l["start"].strftime("%d %b %H:%M") if l["start"] else "—"),
            esc(f"{l['app']} · {l['project']}"),
            fmt_int(l["calls"]), fmt_int(l["tool_calls"]),
            fmt_int(len(l["subagents"])), esc(span),
            fmt_tokens(l["bucket"]["raw_total"]), fmt_cost(l["bucket"]),
        ])
    summary = (
        f"{fmt_int(len(loops))} loops · {sum(calls) / len(loops):.1f} model calls "
        f"per loop on average (median {calls[len(calls) // 2]}, most {calls[-1]}) · "
        f"{fmt_int(tools)} tool calls · {fmt_int(with_sub)} "
        f"{'loop' if with_sub == 1 else 'loops'} used subagents")
    return ("<h2>Agent loops</h2>"
            '<p class="sub">A loop is one prompt you sent and every model call it '
            f"set off until the agent stopped, subagents included. {esc(summary)}."
            " The heaviest:</p>"
            + table(["Started", "App · project", "Model calls", "Tool calls",
                     "Subagents", "Ran for", "Tokens", "Cost"], rows, numeric_from=2))


def tools_html(turns: list[Turn]) -> str:
    stats = tool_stats(turns)
    if not stats:
        return ""
    rows = [[esc(name), esc(" · ".join(sorted(e["apps"]))), fmt_int(e["calls"]),
             fmt_int(e["bucket"]["turns"]), fmt_tokens(e["bucket"]["raw_total"]),
             fmt_cost(e["bucket"])]
            for name, e in sorted(stats.items(), key=lambda kv: -kv[1]["calls"])[:25]]
    return ("<h2>Tools called</h2>"
            '<p class="sub">Every tool the agents called, MCP tools included. '
            "Tokens and cost are those of the model calls that used the tool; one "
            "call can use several tools, so these overlap and do not add up to the "
            "total.</p>"
            + table(["Tool", "App", "Calls", "Model calls", "Tokens", "Cost"],
                    rows, numeric_from=2))


def harness_html(turns: list[Turn]) -> str:
    h = harness_stats(turns)
    blocks = []
    if h["sessions"]:
        blocks.append(
            f"<p><b>Setup per session ≈ {esc(fmt_tokens(h['setup_avg']))} tokens</b>"
            f" on average (median {esc(fmt_tokens(h['setup_median']))}, largest "
            f"{esc(fmt_tokens(h['setup_max']))}, over {fmt_int(h['sessions'])} "
            "sessions). This is the first model call's prompt: the system prompt, "
            "tool and MCP definitions, skills, CLAUDE.md and memory, plus your first "
            "message. It is an estimate of the harness's size, since the logs don't "
            "split it out.</p>"
            f"<p><b>Re-sent history: {h['history_share'] * 100:.0f}% of everything "
            f"read</b> ({esc(fmt_tokens(h['history_reread']))} of "
            f"{esc(fmt_tokens(h['prompt_read']))} tokens) came from the prompt cache: "
            "the harness and the conversation so far, sent again on each call at the "
            "cache-read rate.</p>")
    if h["copilot_parts"]:
        total = h["copilot_prompt"] or 1
        rows = [[esc(label), fmt_tokens(tokens), f"{tokens / total * 100:.0f}%"]
                for label, tokens in h["copilot_parts"].items()]
        blocks.append(
            "<p><b>Copilot's prompts, by part</b> (as Copilot recorded them):</p>"
            + table(["Part", "Tokens", "Share"], rows))
    if not blocks:
        return ""
    return ('<h2>Harness overhead</h2><div class="card formula">'
            + "".join(blocks) + "</div>")


def human_span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


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
        if index % show_every == 0:
            text = label
            try:
                text = dt.date.fromisoformat(label).strftime("%d %b")
            except ValueError:
                pass
            span = slot * show_every
            bars.append(
                f'<div class="bar-label" style="left:{slot * index + slot / 2 - span / 2:.4f}%;'
                f'width:{span:.4f}%">{esc(text)}</div>'
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
            fmt_cost(data),
        ])
    return rows


def calendar_html(turns: list[Turn]) -> str:
    """Month grids, one cell per day. Each cell names the day's total tokens,
    draws a 24-bar graph of when in the day they were used (scaled to that
    day's busiest hour, so the shape reads even on a quiet day), and is shaded
    by how heavy the day was against every other day: five steps of one hue,
    by quantile, so one huge day can't wash out the rest. Hovering or
    focusing a cell shows the day's breakdown."""
    dated = [t for t in turns if t.ts]
    if not dated:
        return '<p class="empty">No dated usage to draw.</p>'
    days: dict[dt.date, dict] = {}
    for turn in dated:
        day = turn.ts.date()
        entry = days.get(day)
        if entry is None:
            entry = days[day] = {"bucket": blank_bucket(), "hours": [0] * 24,
                                 "tools": defaultdict(int),
                                 "models": defaultdict(int)}
        add_turn(entry["bucket"], turn)
        sess = entry.setdefault("sessions", {}).setdefault(
            turn.session or "(none)", {"bucket": blank_bucket(), "turn": turn,
                                       "first": turn.ts, "last": turn.ts,
                                       "models": set(), "loops": set(),
                                       "subagents": set()})
        add_turn(sess["bucket"], turn)
        sess["first"] = min(sess["first"], turn.ts)
        sess["last"] = max(sess["last"], turn.ts)
        sess["models"].add(turn.model_label or "unknown")
        if turn.loop_id:
            sess["loops"].add(turn.loop_id)
        if turn.is_sidechain and turn.agent_id:
            sess["subagents"].add(turn.agent_id)
        if not turn.is_sidechain:
            sess["turn"] = turn                  # the main agent's details
        entry["hours"][turn.ts.hour] += turn.raw_total or 0
        entry["tools"][turn.via or "other"] += turn.raw_total or 0
        entry["models"][turn.model_label or "unknown"] += turn.raw_total or 0

    totals_sorted = sorted(e["bucket"]["raw_total"] for e in days.values())

    def level(value: int) -> int:
        """1-5: which fifth of active days this day's total falls in."""
        if len(totals_sorted) < 2:
            return 3
        rank = sum(1 for v in totals_sorted if v < value)
        return 1 + min(4, rank * 5 // len(totals_sorted))

    first, last = min(days), max(days)

    day_sessions: dict[str, list] = {}
    months = []
    cursor = first.replace(day=1)
    while cursor <= last:
        nxt = (cursor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        month_days = [cursor + dt.timedelta(days=i)
                      for i in range((nxt - cursor).days)]
        month_total = sum(days[d]["bucket"]["raw_total"]
                          for d in month_days if d in days)
        cells = ['<div class="cal-pad"></div>'] * cursor.weekday()
        for day in month_days:
            entry = days.get(day)
            if entry is None:
                cells.append(
                    f'<div class="cal-day cal-empty"><span class="cal-n">'
                    f'{day.day}</span></div>')
                continue
            b = entry["bucket"]
            peak = max(entry["hours"]) or 1
            bars = "".join(
                f'<i style="height:{max(h / peak * 100, 6 if h else 0):.1f}%">'
                f"</i>" for h in entry["hours"])
            tools = " · ".join(f"{k} {fmt_tokens(v)}" for k, v in
                               sorted(entry["tools"].items(), key=lambda kv: -kv[1]))
            models = sorted(entry["models"].items(), key=lambda kv: -kv[1])[:4]
            busiest = max(range(24), key=lambda h: entry["hours"][h])
            tip = {
                "date": day.strftime("%a %d %b %Y"),
                "rows": [
                    ["Tokens", fmt_tokens(b["raw_total"])],
                    ["Turns", fmt_int(b["turns"])],
                    ["Cost", fmt_cost(b)],
                    ["Busiest hour", f"{busiest:02d}:00"],
                ],
                "tools": tools,
                "models": [[m, fmt_tokens(v)] for m, v in models],
            }
            label = (f"{day.strftime('%d %B')}: {fmt_tokens(b['raw_total'])} tokens,"
                     f" {fmt_cost(b)}")
            day_sessions[day.isoformat()] = [
                session_card(sid, info) for sid, info in sorted(
                    entry["sessions"].items(),
                    key=lambda kv: kv[1]["first"])]
            cells.append(
                f'<div class="cal-day lv{level(b["raw_total"])}" tabindex="0" '
                f'role="button" data-day="{day.isoformat()}" '
                f'aria-label="{esc(label)}" '
                f'data-tip="{esc(json.dumps(tip))}">'
                f'<span class="cal-n">{day.day}</span>'
                f'<span class="cal-v">{esc(fmt_tokens(b["raw_total"]))}</span>'
                f'<span class="cal-h" aria-hidden="true">{bars}</span></div>')
        months.append(
            f'<div class="cal-month" data-month="{esc(cursor.strftime("%B %Y"))}">'
            f'<div class="cal-title">'
            f'<button type="button" class="cal-prev" aria-label="Previous month">'
            f"‹</button>"
            f'<b>{esc(cursor.strftime("%B %Y"))}</b>'
            f'<button type="button" class="cal-next" aria-label="Next month">'
            f"›</button>"
            f"<span>{esc(fmt_tokens(month_total))} tokens</span></div>"
            f'<div class="cal-grid">'
            + "".join(f'<div class="cal-wd">{w}</div>' for w in
                      ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
            + "".join(cells) + "</div></div>")
        cursor = nxt
    # Oldest to newest, so ‹ goes back in time; the newest is shown first.
    swatches = "".join(f'<i class="lv{n}"></i>' for n in range(1, 6))
    return (
        f'<div class="cal-key"><span>Each day shows its total tokens and when in '
        f"the day they were used, one bar per hour from 00:00 to 23:00, scaled to "
        f"that day's busiest hour. Use ‹ › (or the arrow keys) to change month, and "
        f"hover or tab to a day for its breakdown.</span>"
        f'<span class="cal-legend">lighter day{swatches}heavier day</span></div>'
        f'<div class="cal">{"".join(months)}</div>'
        f'<div class="cal-tip" role="tooltip" hidden></div>'
        '<aside class="drawer" id="drawer" role="dialog" aria-labelledby="drawer-title" '
        'hidden><div class="drawer-grip" role="separator" aria-orientation="vertical" '
        'aria-label="Resize panel" tabindex="0"></div><div class="drawer-head"><div><p class="drawer-k">Sessions on</p>'
        '<h3 id="drawer-title"></h3></div><button type="button" class="drawer-x" '
        'aria-label="Close">✕</button></div><div class="drawer-body"></div></aside>'
        '<script type="application/json" id="cal-sessions">'
        + json.dumps(day_sessions).replace("</", "<\\/") + "</script>"
    )


def session_card(session: str, info: dict) -> dict:
    """What the side panel shows for one session on one day, and how to
    carry on with it."""
    turn: Turn = info["turn"]
    b = info["bucket"]
    cwd = turn.cwd or ""
    folder_ok = bool(cwd) and os.path.isdir(cwd)
    resume = ""
    how = ""
    if turn.via in ("Claude Code", "Agent SDK") and session != "(none)":
        resume = (f"cd {shlex.quote(cwd)} && claude --resume {session}"
                  if cwd else f"claude --resume {session}")
        how = ("Run this in a terminal to reopen the conversation where it left "
               "off. It works for sessions from the terminal, VS Code and the "
               "desktop app.")
    elif turn.via == "Copilot":
        how = ("Open the folder in VS Code, then pick this chat from the Copilot "
               "chat history (the clock icon in the Chat view).")
    elif turn.via == "Cowork":
        how = "Reopen it from Cowork in the Claude desktop app."
    elif turn.via == "Custom agent":
        how = "Logged by agent_tracer; your agent decides how a session resumes."
    return {
        "id": session,
        "title": turn.title or "(untitled)",
        "named": bool(turn.named),
        "app": turn.via or "",
        "surface": turn.surface or "",
        "project": turn.project or "",
        "cwd": cwd,
        "folder_ok": folder_ok,
        "time": f"{info['first'].strftime('%H:%M')}–{info['last'].strftime('%H:%M')}",
        "calls": fmt_int(b["turns"]),
        "tokens": fmt_tokens(b["raw_total"]),
        "cost": fmt_cost(b),
        "models": sorted(info["models"]),
        "loops": len(info["loops"]),
        "subagents": len(info["subagents"]),
        "resume": resume,
        "how": how,
        "vscode": ("vscode://file" + url_quote(cwd)) if folder_ok else "",
    }


CAL_JS = """
(function(){
  // One month at a time; ‹ › step through them, newest first.
  var months=[].slice.call(document.querySelectorAll('.cal-month'));
  var at=months.length-1;
  function go(i){
    if(i<0||i>=months.length) return;
    at=i;
    months.forEach(function(m,j){m.hidden=(j!==at);
      m.querySelector('.cal-prev').disabled=(j===0);
      m.querySelector('.cal-next').disabled=(j===months.length-1);});
  }
  months.forEach(function(m){
    m.querySelector('.cal-prev').addEventListener('click',function(){
      go(at-1); months[at].querySelector('.cal-prev').focus();});
    m.querySelector('.cal-next').addEventListener('click',function(){
      go(at+1); months[at].querySelector('.cal-next').focus();});
  });
  document.addEventListener('keydown',function(e){
    var t=e.target.tagName; if(t==='INPUT'||t==='TEXTAREA') return;
    var cal=document.querySelector('.cal'); if(!cal) return;
    var r=cal.getBoundingClientRect();
    if(r.bottom<0||r.top>window.innerHeight) return;   // only when in view
    if(e.key==='ArrowLeft'){go(at-1);} else if(e.key==='ArrowRight'){go(at+1);}
  });
  go(at);
})();
(function(){
  var tip=document.querySelector('.cal-tip'); if(!tip) return;
  function el(tag,cls,text){var e=document.createElement(tag);
    if(cls)e.className=cls; if(text!=null)e.textContent=text; return e;}
  function show(cell){
    var d; try{d=JSON.parse(cell.getAttribute('data-tip'));}catch(e){return;}
    tip.textContent='';
    tip.appendChild(el('div','t-date',d.date));
    d.rows.forEach(function(r){var row=el('div','t-row');
      row.appendChild(el('span',null,r[0]));row.appendChild(el('b',null,r[1]));
      tip.appendChild(row);});
    if(d.tools) tip.appendChild(el('div','t-sub',d.tools));
    d.models.forEach(function(m){var row=el('div','t-row t-model');
      row.appendChild(el('span',null,m[0]));row.appendChild(el('b',null,m[1]));
      tip.appendChild(row);});
    tip.hidden=false;
    var r=cell.getBoundingClientRect(), w=tip.offsetWidth, h=tip.offsetHeight;
    var x=Math.min(Math.max(8,r.left+r.width/2-w/2),window.innerWidth-w-8);
    var y=r.top-h-8; if(y<8) y=r.bottom+8;
    tip.style.left=x+'px'; tip.style.top=y+'px';
  }
  function hide(){tip.hidden=true;}
  document.querySelectorAll('.cal-day[data-tip]').forEach(function(c){
    c.addEventListener('mouseenter',function(){show(c);});
    c.addEventListener('mouseleave',hide);
    c.addEventListener('focus',function(){show(c);});
    c.addEventListener('blur',hide);
  });
  document.addEventListener('keydown',function(e){if(e.key==='Escape')hide();});
  window.addEventListener('scroll',hide,{passive:true});
})();
(function(){
  // Click a day: a side panel lists that day's sessions and how to resume.
  var drawer=document.getElementById('drawer'); if(!drawer) return;
  var data={}; try{data=JSON.parse(document.getElementById('cal-sessions').textContent);}catch(e){}
  var body=drawer.querySelector('.drawer-body'), title=drawer.querySelector('#drawer-title');
  var opener=null;
  function el(tag,cls,text){var e=document.createElement(tag);
    if(cls)e.className=cls; if(text!=null)e.textContent=text; return e;}
  function copy(text,btn){
    function done(){var t=btn.textContent;btn.textContent='Copied';
      setTimeout(function(){btn.textContent=t;},1400);}
    if(navigator.clipboard&&window.isSecureContext!==false){
      navigator.clipboard.writeText(text).then(done,fallback);} else fallback();
    function fallback(){var a=document.createElement('textarea');a.value=text;
      document.body.appendChild(a);a.select();
      try{document.execCommand('copy');done();}catch(e){}
      document.body.removeChild(a);}
  }
  function card(s){
    var c=el('article','s-card');
    var h=el('div','s-top');
    h.appendChild(el('span','s-app',s.app));
    if(s.named) h.appendChild(el('span','s-named','Named'));
    h.appendChild(el('span','s-time',s.time));
    c.appendChild(h);
    c.appendChild(el('h4','s-title',s.title));
    c.appendChild(el('p','s-where',s.surface+' · '+s.project));
    var g=el('dl','s-grid');
    [['Model calls',s.calls],['Tokens',s.tokens],['Cost',s.cost],
     ['Loops',String(s.loops)]].concat(s.subagents?[['Subagents',String(s.subagents)]]:[])
     .forEach(function(r){var d=el('div');d.appendChild(el('dt',null,r[0]));
       d.appendChild(el('dd',null,r[1]));g.appendChild(d);});
    c.appendChild(g);
    c.appendChild(el('p','s-models',s.models.join(' · ')));
    if(s.resume){
      var box=el('div','s-cmd'); var code=el('code',null,s.resume);
      var b=el('button','s-btn','Copy resume command'); b.type='button';
      b.addEventListener('click',function(){copy(s.resume,b);});
      box.appendChild(code); c.appendChild(box); c.appendChild(b);
    }
    if(s.vscode){var a=el('a','s-btn s-link','Open folder in VS Code');a.href=s.vscode;
      c.appendChild(a);}
    if(s.how) c.appendChild(el('p','s-how',s.how));
    if(s.cwd && !s.folder_ok) c.appendChild(el('p','s-how','The folder '+s.cwd+
      ' no longer exists, so resuming may need to start from another folder.'));
    var id=el('p','s-id','Session '+s.id); c.appendChild(id);
    return c;
  }
  function open(day,cell){
    var list=data[day]||[]; opener=cell;
    var d=new Date(day+'T12:00:00');
    title.textContent=d.toLocaleDateString(undefined,{weekday:'long',day:'numeric',month:'long',year:'numeric'});
    body.textContent='';
    if(!list.length) body.appendChild(el('p','s-how','Nothing recorded on this day.'));
    list.forEach(function(s){body.appendChild(card(s));});
    drawer.hidden=false; document.body.classList.add('drawer-open');
    var tip=document.querySelector('.cal-tip'); if(tip) tip.hidden=true;
    drawer.querySelector('.drawer-x').focus();
  }
  function close(){drawer.hidden=true; document.body.classList.remove('drawer-open');
    if(opener) opener.focus();}
  drawer.querySelector('.drawer-x').addEventListener('click',close);
  // Drag (or arrow-key) the left edge to resize; the page reflows beside it.
  var grip=drawer.querySelector('.drawer-grip'), root=document.documentElement;
  function clamp(w){return Math.round(Math.max(300,Math.min(w,window.innerWidth-320,960)));}
  function setW(w,save){w=clamp(w);root.style.setProperty('--drawer-w',w+'px');
    grip.setAttribute('aria-valuenow',w);
    if(save){try{localStorage.setItem('tt-drawer-w',w);}catch(e){}}}
  try{var saved=parseInt(localStorage.getItem('tt-drawer-w'),10);if(saved)setW(saved);}catch(e){}
  grip.addEventListener('pointerdown',function(e){
    e.preventDefault(); grip.setPointerCapture(e.pointerId);
    document.body.classList.add('drawer-drag');
    function move(ev){setW(window.innerWidth-ev.clientX);}
    function up(ev){grip.removeEventListener('pointermove',move);
      grip.removeEventListener('pointerup',up);grip.removeEventListener('pointercancel',up);
      document.body.classList.remove('drawer-drag');
      setW(window.innerWidth-ev.clientX,true);}
    grip.addEventListener('pointermove',move);
    grip.addEventListener('pointerup',up);grip.addEventListener('pointercancel',up);
  });
  grip.addEventListener('keydown',function(e){
    var w=drawer.getBoundingClientRect().width;
    if(e.key==='ArrowLeft'){e.preventDefault();setW(w+24,true);}
    else if(e.key==='ArrowRight'){e.preventDefault();setW(w-24,true);}
  });
  grip.addEventListener('dblclick',function(){setW(440,true);});
  window.addEventListener('resize',function(){
    if(root.style.getPropertyValue('--drawer-w'))setW(parseInt(root.style.getPropertyValue('--drawer-w'),10));});
  document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!drawer.hidden)close();});
  document.querySelectorAll('.cal-day[data-day]').forEach(function(c){
    c.addEventListener('click',function(){open(c.getAttribute('data-day'),c);});
    c.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){
      e.preventDefault();open(c.getAttribute('data-day'),c);}});
  });
})();
"""


CSS = """:root{color-scheme:light dark;
--bg:#faf9f7;--card:#ffffff;--ink:#141413;--ink2:#5f5e5a;--ink3:#8a8880;
--line:#e5e3dc;--accent:#2a78d6;--accent2:#b5d4f4;--warn:#854f0b;--warnbg:#faeeda;
--drawer-w:440px}
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
.cal-day[data-day]{cursor:pointer}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(var(--drawer-w),100vw);z-index:20;
background:var(--bg);border-left:1px solid var(--line);
box-shadow:-12px 0 32px rgba(0,0,0,.14);display:flex;flex-direction:column}
.drawer[hidden]{display:none}
.drawer-grip{position:absolute;top:0;bottom:0;left:-5px;width:10px;cursor:col-resize;
z-index:1;touch-action:none}
.drawer-grip::after{content:"";position:absolute;top:0;bottom:0;left:4px;width:2px;
background:transparent;transition:background .12s}
.drawer-grip:hover::after,.drawer-grip:focus-visible::after,
.drawer-drag .drawer-grip::after{background:var(--accent)}
.drawer-grip:focus-visible{outline:none}
.drawer-drag{cursor:col-resize;user-select:none;-webkit-user-select:none}
@media (min-width:900px){body.drawer-open{padding-right:var(--drawer-w)}
body.drawer-open .drawer{box-shadow:none}}
.drawer-head{display:flex;justify-content:space-between;align-items:flex-start;
gap:12px;padding:20px 20px 14px;border-bottom:1px solid var(--line)}
.drawer-k{margin:0;font-size:11.5px;color:var(--ink3);text-transform:uppercase;
letter-spacing:.04em}
.drawer-head h3{margin:2px 0 0;font-size:17px;font-weight:600}
.drawer-x{width:32px;height:32px;border-radius:8px;border:1px solid var(--line);
background:var(--card);color:var(--ink);cursor:pointer;font-size:14px;flex:none}
.drawer-x:hover{border-color:var(--accent);color:var(--accent)}
.drawer-body{overflow-y:auto;overflow-x:hidden;padding:14px 20px 28px;display:grid;
grid-template-columns:minmax(0,1fr);gap:12px;align-content:start}
.s-card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:14px 14px 12px;min-width:0}
.s-top{display:flex;align-items:center;gap:8px;font-size:12px}
.s-app{font-weight:600;color:var(--accent)}
.s-named{border:1px solid var(--line);border-radius:999px;padding:0 8px;
color:var(--ink2);font-size:11px}
.s-time{margin-left:auto;color:var(--ink3);font-variant-numeric:tabular-nums}
.s-title{margin:6px 0 2px;font-size:15px;font-weight:600;line-height:1.35}
.s-where,.s-models{margin:0;font-size:12.5px;color:var(--ink2)}
.s-models{color:var(--ink3);margin-top:8px}
.s-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(80px,1fr));
gap:8px;margin:10px 0 0}
.s-grid dt{font-size:11px;color:var(--ink3)}
.s-grid dd{margin:0;font-weight:600;font-variant-numeric:tabular-nums}
.s-cmd{margin-top:12px;background:color-mix(in srgb,var(--ink) 6%,transparent);
border-radius:8px;padding:8px 10px;overflow-x:auto;max-width:100%}
.s-cmd code{background:none;padding:0;font-size:12px;white-space:pre}
.s-btn{display:inline-flex;margin:8px 8px 0 0;padding:6px 12px;border-radius:8px;
border:1px solid var(--line);background:var(--card);color:var(--ink);font:inherit;
font-size:12.5px;cursor:pointer;text-decoration:none}
.s-btn:hover{border-color:var(--accent);color:var(--accent)}
.s-btn:focus-visible,.cal-day:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.s-how{margin:10px 0 0;font-size:12px;color:var(--ink2);line-height:1.5}
.s-id{margin:8px 0 0;font-size:11px;color:var(--ink3);word-break:break-all}
.formula p{margin:0 0 10px;font-size:13.5px}
.formula p.x{margin:10px 0 0;color:var(--ink3);font-size:12px}
.formula td{white-space:normal;vertical-align:top;line-height:1.5}
.formula td.num{white-space:nowrap}
.formula a{color:var(--accent)}
.cal-key{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px 20px;
color:var(--ink2);font-size:12.5px;margin:0 0 12px}
.cal-legend{display:inline-flex;align-items:center;gap:3px;color:var(--ink3);
white-space:nowrap}
.cal-legend i{width:16px;height:12px;border-radius:3px;border:1px solid var(--line);
display:inline-block}
.cal-legend i:first-child{margin-left:6px}.cal-legend i:last-child{margin-right:6px}
.cal{display:grid;gap:14px}
.cal-month{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:14px 14px 12px}
.cal-title{display:flex;align-items:center;gap:6px;font-size:15px;margin:0 0 12px}
.cal-title b{font-weight:600;min-width:9.5em;text-align:center}
.cal-title span{margin-left:auto;font-size:12px;color:var(--ink3);
font-variant-numeric:tabular-nums}
.cal-title button{width:32px;height:32px;border-radius:8px;border:1px solid var(--line);
background:var(--card);color:var(--ink);font-size:20px;line-height:1;cursor:pointer;
display:inline-flex;align-items:center;justify-content:center;padding:0 0 2px}
.cal-title button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
.cal-title button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.cal-title button:disabled{opacity:.35;cursor:default}
.cal-month[hidden]{display:none}
.cal-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}
.cal-wd{font-size:10px;color:var(--ink3);text-align:center;text-transform:uppercase;
letter-spacing:.04em}
.cal-day{position:relative;min-height:92px;border-radius:8px;
background:var(--card);border:1px solid var(--line);padding:6px 8px;
display:flex;flex-direction:column;min-width:0;cursor:default;outline:none}
.lv1{background:color-mix(in srgb,var(--accent) 6%,var(--card))}
.lv2{background:color-mix(in srgb,var(--accent) 13%,var(--card))}
.lv3{background:color-mix(in srgb,var(--accent) 21%,var(--card))}
.lv4{background:color-mix(in srgb,var(--accent) 30%,var(--card))}
.lv5{background:color-mix(in srgb,var(--accent) 40%,var(--card))}
.cal-day[data-tip]:hover,.cal-day[data-tip]:focus-visible{border-color:var(--accent);
box-shadow:0 0 0 1px var(--accent)}
.cal-empty{background:transparent;border-style:dashed;opacity:.55}
.cal-n{font-size:11px;line-height:1;color:var(--ink2);font-variant-numeric:tabular-nums}
.cal-v{font-size:14px;line-height:1.3;font-weight:600;margin-top:3px;
font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}
.cal-h{flex:1;display:flex;align-items:flex-end;gap:1px;margin-top:4px;
border-bottom:1px solid color-mix(in srgb,var(--ink) 25%,transparent);min-height:26px}
.cal-h i{flex:1;background:var(--accent);border-radius:2px 2px 0 0;display:block}
.cal-tip{position:fixed;z-index:10;pointer-events:none;background:var(--card);
color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px 12px;
font-size:12.5px;line-height:1.45;min-width:210px;max-width:300px;
box-shadow:0 6px 24px rgba(0,0,0,.14)}
.cal-tip .t-date{font-weight:600;margin-bottom:4px}
.cal-tip .t-row{display:flex;justify-content:space-between;gap:14px}
.cal-tip .t-row span{color:var(--ink2)}
.cal-tip .t-row b{font-weight:600;font-variant-numeric:tabular-nums}
.cal-tip .t-sub{color:var(--ink3);margin:6px 0 2px;border-top:1px solid var(--line);
padding-top:6px}
.cal-tip .t-model span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:640px){.cal-day{min-height:64px;padding:4px}
.cal-v{font-size:11px}.cal-h{min-height:16px}.cal-grid{gap:3px}}
"""


def cost_formula_html(turns: list[Turn], pricing: Pricing) -> str:
    """The bottom-of-page key: how each platform's dollars were worked out,
    with this report's own numbers plugged in."""
    rows = []
    for name, b in cost_by_tool(turns):
        if name == "Copilot":
            rate = pricing.copilot_usd_per_credit
            if rate is None:
                calc = ("Copilot's AI credits could not be converted: GitHub's "
                        "credit rate has not been downloaded yet.")
            else:
                calc = (f"{fmt_credits(b['credits'])} recorded by Copilot "
                        f"× ${rate:g} per credit = <b>{esc(fmt_money(b['credits'] * rate))}"
                        f"</b>")
            stated = (f"set 1 AI credit = ${rate:g} USD" if rate is not None
                      else "give the dollar value of a credit")
            how = (f"cost = AI credits × USD per credit. Copilot records what it "
                   f"charged for each request in GitHub AI credits, and GitHub's "
                   f'<a href="{esc(GITHUB_CREDIT_PAGE)}">billing docs</a> {stated}.')
        else:
            calc = (f"{fmt_tokens(b['input'])} input · {fmt_tokens(b['cache_read'])} "
                    f"cache reads · {fmt_tokens(b['cache_write'])} cache writes · "
                    f"{fmt_tokens(b['output'])} output → <b>{esc(fmt_cost(b))}</b>")
            how = ("cost = input × input rate + cache reads × cache-read rate + "
                   "5-minute cache writes × 5m rate + 1-hour cache writes × 1h rate + "
                   "output × output rate (+ $ per web search), for each turn, at "
                   f'<a href="{esc(ANTHROPIC_PRICING_URL[:-3])}">Anthropic\'s list '
                   f"prices</a> per million tokens for that turn's model, with fast-mode, "
                   f"batch and US-only rates where the turn used them.")
        rows.append(f"<tr><td><b>{esc(name)}</b></td><td>{how}</td>"
                    f"<td class=\"num\">{calc}</td></tr>")
    total = totals(turns)
    return (
        '<h2 id="how-cost">How cost is calculated</h2>'
        '<div class="card formula"><p>Every cost on this page is in <b>US dollars '
        f"(USD)</b>. Total = {' + '.join(esc(n) for n, _ in cost_by_tool(turns))}"
        f" = <b>{esc(fmt_cost(total))}</b>.</p>"
        '<div class="scroll"><table><thead><tr><th>Platform</th><th>Formula</th>'
        '<th class="num">This report</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
        f'<p class="x">Prices fetched {esc(pricing.live_checked)}. A cost shown as '
        f"“—” has no published rate; a total ending in “+” leaves such turns out. "
        f"This is list price: on a subscription you are not billed per token.</p>"
        "</div>"
    )


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

    any_priced = dollar_turns(grand) > 0
    tiles = [
        ("Total cost (USD)", fmt_cost(grand),
         f"{tool_split(turns)} · {fmt_money(grand['cost'] / active_days)}/active day"
         if any_priced else "no published rate for these models"),
        ("Tokens billed", fmt_tokens(grand["raw_total"]),
         f"{fmt_tokens(grand['output'])} written by Claude"),
        ("Turns", fmt_int(grand["turns"]),
         f"{fmt_int(len(grand['sessions']))} sessions"
         + (f" · {fmt_int(sidechain_turns)} subagent" if sidechain_turns else "")),
    ]
    if any_priced:
        tiles.append(("Saved by caching", fmt_money(saved),
                      "vs. re-reading at full price"))
    if grand["searches"]:
        per_1k = pricing.web_search_per_1k
        tiles.append((
            "Web searches", fmt_int(grand["searches"]),
            fmt_money(grand["searches"] / 1000 * per_1k) if per_1k is not None
            else "no published rate",
        ))

    tile_html = "".join(
        f'<div class="tile"><p class="k">{esc(k)}</p><p class="v">{esc(v)}</p>'
        f'<p class="x">{esc(x)}</p></div>'
        for k, v, x in tiles
    )

    sections = []

    sections.append(f"<h2>Calendar</h2>{calendar_html(turns)}")

    headers = ["", "Turns", "Tokens", "Output", "Cost"]

    for title, field, limit, alpha in [
        ("By model", "model_label", None, False),
        ("By app", "via", None, False),
        ("By surface", "surface", None, False),
        ("By project", "project", 15, False),
        ("Heaviest sessions", "session", 15, False),
        ("By day", "day", None, True),
    ]:
        buckets = aggregate(turns, field)
        label = {"model_label": "Model", "via": "App",
                 "surface": "Where you were working",
                 "project": "Project", "session": "Session", "day": "Day"}[field]
        sections.append(
            f"<h2>{esc(title)}</h2>"
            f"{table([label] + headers[1:], bucket_rows(buckets, limit=limit, sort_alpha=alpha))}"
        )

    sections.append(agents_html(turns))
    sections.append(loops_html(turns))
    sections.append(tools_html(turns))
    sections.append(harness_html(turns))

    combos: dict[tuple[str, str], dict] = defaultdict(blank_bucket)
    for turn in turns:
        add_turn(combos[(turn.surface or "(unknown)",
                         turn.model_label or "unknown")], turn)
    combo_rows = [
        [esc(surface), esc(model), fmt_int(b["turns"]), fmt_tokens(b["raw_total"]),
         fmt_tokens(b["output"]), fmt_cost(b)]
        for (surface, model), b in sorted(
            combos.items(), key=lambda kv: (kv[0][0], -kv[1]["raw_total"]))
    ]
    sections.append(
        "<h2>By platform and model</h2>"
        + table(["Platform", "Model", "Turns", "Tokens", "Output", "Cost"],
                combo_rows, numeric_from=2))

    for title, field, label in [
        ("Copilot by mode", "mode", "Mode · chat location"),
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
            f'<div class="note"><b>{len(pricing.unknown_models)} model(s) have no '
            f"published price yet</b> ({esc(listed)}). Their tokens are counted, "
            f"and their cost is left blank rather than guessed. It fills in once "
            f"the live price lists include them, or when you add the rates to "
            f"<code>{esc(str(USER_PRICING))}</code>.</div>"
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
<title>Token usage</title><style>{CSS}</style></head><body>
<div class="wrap">
<h1>Token usage</h1>
<p class="sub">{esc(window_label)} · generated {esc(generated)} ·
{fmt_int(stats['files'])} transcript files read</p>

<div class="note">
<b>What this covers.</b> Every turn from Claude Code in the terminal, the editor
extensions, and Cowork — read straight from the transcripts on this machine, so the
token counts are exact.<br>
<b>GitHub Copilot.</b> Copilot Chat in VS Code is read from its local chat logs,
whichever model you picked, Claude included, and shown as "Copilot · &lt;model&gt;".
Its cost is what Copilot recorded charging, converted to dollars. Its logs keep only
each request's final prompt, so Copilot input counts are a floor.<br>
<b>What it cannot cover.</b> claude.ai in the browser, the desktop chat window, and
the mobile apps keep no transcript on disk and expose no per-conversation usage API.
Those are missing here entirely — check Settings → Usage for them.<br>
<b>All costs are in US dollars.</b> See <a href="#how-cost">how cost is
calculated</a> at the bottom. If you
are on a subscription plan you are not billed per token, so read cost as relative
weight, not as an invoice.{busiest_line}
</div>

{unknown_note}

<div class="tiles">{tile_html}</div>

{''.join(sections)}

{cost_formula_html(turns, pricing)}

<footer>
Scanned: {esc(', '.join(str(r) for r in roots)) or '(nothing found)'}<br>
{fmt_int(stats['lines'])} transcript lines · {fmt_int(stats['duplicates'])} duplicate
requests skipped · {fmt_int(stats['bad_lines'])} unreadable lines<br>
Prices from <a href="{esc(ANTHROPIC_PRICING_URL[:-3])}">Anthropic's
pricing page</a> and the public LiteLLM price list, refreshed daily.
Cache writes are priced at their real 5-minute or 1-hour rate where the transcript
records which was used.<br>
claude-token-tracker · reads only local files; downloads the public price lists, sends nothing.
</footer>
</div><script>{CAL_JS}</script></body></html>"""


# --------------------------------------------------------------------------- #
# terminal output
# --------------------------------------------------------------------------- #

def print_summary(turns: list[Turn], stats: dict, pricing: Pricing,
                  window_label: str) -> None:
    grand = totals(turns)
    print()
    print(f"  Token usage — {window_label}")
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
    print(f"  Total cost (USD)   {fmt_cost(grand)}")
    for name, b in cost_by_tool(turns):
        extra = ""
        if name == "Copilot" and b["credit_turns"] and pricing.copilot_usd_per_credit:
            extra = (f"   = {fmt_credits(b['credits'])} ×"
                     f" ${pricing.copilot_usd_per_credit:g}")
        print(f"    {name:<16} {fmt_cost(b)}{extra}")
    if dollar_turns(grand) > 0:
        print(f"  Saved by caching   {fmt_money(cache_savings(turns, pricing))}")
    if grand["unpriced"]:
        print(f"  ! {fmt_int(grand['unpriced'])} turns have no published price;"
              f" their cost is not shown")
    print()

    for title, field in [("By model", "model_label"), ("By app", "via"),
                         ("By agent", "agent_label"),
                         ("By surface", "surface")]:
        buckets = aggregate(turns, field)
        if not buckets:
            continue
        print(f"  {title}")
        for name, data in sorted(buckets.items(), key=lambda kv: -kv[1]["cost"]):
            print(f"    {name[:34]:<34} {fmt_tokens(data['raw_total']):>9}"
                  f" {fmt_cost(data):>10}")
        print()


def cell_cost(turn: Turn) -> str:
    return fmt_money(turn.cost if turn.priced else None)


def print_session_detail(turns: list[Turn], session_id: str,
                         pricing: Pricing) -> None:
    picked = [t for t in turns if t.session.startswith(session_id)]
    if not picked:
        print(f"No turns found for session starting '{session_id}'.")
        return
    picked.sort(key=lambda t: t.ts or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    print()
    print(f"  Session {picked[0].session}")
    print(f"  {picked[0].project} · {picked[0].surface} · "
          f"{picked[0].model_label}"
          + (f" · {picked[0].mode}" if picked[0].mode else ""))
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
              f" {fmt_tokens(turn.output):>8}"
              f" {cell_cost(turn):>9}{flag}")
    print("  " + "-" * 74)
    priced = [t for t in picked if t.priced]
    credits = [t.credits for t in picked if t.credits is not None]
    if priced:
        partial = "+" if len(priced) < len(picked) else ""
        print(f"  {len(picked)} turns · {fmt_money(running)}{partial} total"
              f" · {fmt_money(running / len(priced))} per priced turn")
    else:
        print(f"  {len(picked)} turns · no published price for this model")
    if picked[0].via == "Copilot":
        print(f"  Cost = {fmt_credits(sum(credits))} recorded by Copilot ×"
              f" ${pricing.copilot_usd_per_credit or 0:g} per credit (GitHub)."
              if credits and pricing.copilot_usd_per_credit else
              "  Copilot's credits could not be converted to dollars yet.")
        print("  Copilot logs each request's final prompt only, so input is a floor.")
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
                        help="your own rates file (default ~/.claude/token-tracker/pricing.json)")
    parser.add_argument("--no-subagents", action="store_true",
                        help="exclude subagent (sidechain) turns")
    parser.add_argument("--no-copilot", action="store_true",
                        help="leave out GitHub Copilot Chat usage")
    parser.add_argument("--utc", action="store_true",
                        help="bucket days by UTC instead of local time")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    pricing_path = Path(os.path.expanduser(args.pricing))
    if args.pricing != str(DEFAULT_PRICING) and not pricing_path.is_file():
        print(f"Pricing file not found: {pricing_path}", file=sys.stderr)
        return 2
    pricing = Pricing(pricing_path)
    if not pricing.has_rates:
        print("No prices available: the live price lists could not be fetched "
              f"({pricing.remote_error or 'offline'}). Token counts are shown "
              "without cost.", file=sys.stderr)

    roots = discover_roots(args.root)
    copilot_found = False
    if not args.no_copilot:
        import copilot_usage
        copilot_found = bool(copilot_usage.session_files())
    if not roots and not copilot_found:
        print(
            "No Claude transcript directory or Copilot chat log found.\n"
            "Looked for ~/.claude/projects. If your transcripts live elsewhere, "
            "pass --root /path/to/projects.",
            file=sys.stderr,
        )
        return 1

    turns, stats = collect_turns(
        roots, pricing, use_utc=args.utc,
        include_sidechains=not args.no_subagents,
        include_copilot=not args.no_copilot,
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

    if args.all:
        first = min((t.ts for t in turns if t.ts), default=None)
        if first:
            window = f"All time · since {first.strftime('%d %b %Y')}"

    if not turns:
        print(f"No usage found for: {window}.", file=sys.stderr)
        print(f"Scanned {stats['files']} transcript files under "
              f"{', '.join(str(r) for r in roots)}.", file=sys.stderr)
        return 0

    if args.session:
        print_session_detail(turns, args.session, pricing)
        return 0

    if args.as_json:
        def clean(buckets: dict) -> dict:
            return {
                key: {**{k: v for k, v in data.items() if k != "sessions"},
                      "sessions": len(data["sessions"]),
                      "cost": bucket_cost(data),
                      "copilot_credits": bucket_credits(data)}
                for key, data in buckets.items()
            }

        grand = totals(turns)
        payload = {
            "window": window,
            "generated": dt.datetime.now().isoformat(timespec="seconds"),
            "coverage": {
                "included": ["Claude Code (terminal)", "editor extensions", "Cowork",
                             "GitHub Copilot Chat in VS Code"],
                "excluded": ["claude.ai web", "desktop chat UI", "mobile apps",
                             "Copilot in JetBrains / Visual Studio / CLI"],
                "reason": "those surfaces write no local transcript and expose no "
                          "per-conversation usage API",
            },
            "pricing_fetched": pricing.live_checked,
            "currency": "USD",
            "copilot_usd_per_credit": pricing.copilot_usd_per_credit,
            "scan": {k: v for k, v in stats.items()},
            "totals": {**{k: v for k, v in grand.items() if k != "sessions"},
                       "sessions": len(grand["sessions"]),
                       "cost": bucket_cost(grand),
                       "copilot_credits": bucket_credits(grand),
                       "cache_savings": (round(cache_savings(turns, pricing), 6)
                                         if bucket_cost(grand) is not None
                                         else None)},
            "by_day": clean(aggregate(turns, "day")),
            "by_model": clean(aggregate(turns, "model_label")),
            "by_app": clean(aggregate(turns, "via")),
            "by_agent": clean(aggregate(turns, "agent_label")),
            "loops": [{"loop": l["loop"], "app": l["app"], "project": l["project"],
                       "session": l["session"],
                       "started": l["start"].isoformat() if l["start"] else None,
                       "model_calls": l["calls"], "tool_calls": l["tool_calls"],
                       "subagents": len(l["subagents"]),
                       "tokens": l["bucket"]["raw_total"],
                       "cost": bucket_cost(l["bucket"])}
                      for l in loop_stats(turns)[:50]],
            "tools": {name: {"calls": e["calls"], "model_calls": e["bucket"]["turns"],
                             "tokens": e["bucket"]["raw_total"],
                             "cost": bucket_cost(e["bucket"])}
                      for name, e in tool_stats(turns).items()},
            "harness": harness_stats(turns),
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
                  f" {fmt_cost(data):>10}")
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
