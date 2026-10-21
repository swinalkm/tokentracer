---
name: token-tracker
description: Report Claude token usage and cost from local transcripts, and run a live desktop panel that counts tokens as they are spent. Use when the user asks how many tokens they used, what a session or day cost, where their tokens went, which model or project is most expensive, whether caching is helping, or asks for a usage dashboard or usage report. Also use for "am I burning through my limit", "why is this session so expensive", per-session or per-turn breakdowns, and for anything about watching or tracking token usage live, in real time, on screen, on the desktop, in a widget, panel, monitor, or always-on display.
---

# Token tracker

Reports exact per-turn token usage and its cost at live list prices by reading the JSONL transcripts that Claude writes locally, and can keep a live panel on the user's desktop that updates as each turn lands. Everything runs on the user's machine; nothing is uploaded.

Two tools, for two different questions:

| The user wants | Use |
| --- | --- |
| To watch spending happen, now | `scripts/tokenwatch.py` — the desktop panel |
| To know what happened, and why | `scripts/token_report.py` — the report |

## What is and is not covered

Say this plainly whenever you report numbers — omitting it makes the figures misleading.

**Covered, exactly:** Claude Code in the terminal, the VS Code and JetBrains extensions, Cowork, the Agent SDK, and any other surface that writes to `~/.claude/projects/`. Token counts come from the API's own `usage` field, so they are not estimates.

**GitHub Copilot Chat in VS Code / VS Code Insiders** is included, for any model, Claude included. It is read from Copilot's local chat logs. It shows as `Copilot · <model>` (for example `Copilot · Sonnet 4.5`), surface `Copilot · VS Code`, tool `Copilot`, and a mode such as `agent mode · panel`. Its cost is in dollars: the GitHub AI credits Copilot recorded charging × GitHub's published rate (1 AI credit = $0.01 USD, downloaded daily). Copilot logs no per-call prompts or cache figures, so its input count is a floor. All costs are USD. Quote totals with their platform split, for example "Claude Code $23.13 · Copilot $1.40". Copilot in JetBrains, Visual Studio and the Copilot CLI is not covered, because they store no token counts. `--no-copilot` leaves Copilot out.

**Not covered at all:** claude.ai in the browser, the desktop chat window, and the mobile apps. Those keep no transcript on disk and expose no per-conversation usage API. There is no workaround — do not attempt to infer them. Point the user to Settings → Usage.

**Cost is tokens × live list prices.** Rates are downloaded daily from Anthropic's pricing page, and from the public LiteLLM list for other models. The user's own `~/.claude/token-tracker/pricing.json` takes priority over both. Nothing is hard-coded or estimated. A turn whose rates are unknown shows "—", and a total that leaves such turns out ends in "+". On a Pro, Max, Team, or Enterprise subscription the user is not billed per token, so present cost as relative weight rather than an amount owed. Only on API/Console billing does it approximate a real charge.

## The live panel

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" tokenwatch.py [on|off|status|auto-on|auto-off|install-app]
```

With no argument it toggles. It opens a small always-on-top window — a real
desktop window via Tkinter, not a browser page — that tails the transcripts and
repaints within a second of each new turn, subagent turns included. `/tokens-watch`
is the slash command for it.

Offer it, once, when the user says they want to watch usage live, keep an eye on
it while they work, or see the cost of each request as it happens. A report
answers a question; the panel answers "how do I keep seeing this". Do not push it
on someone who just asked for a number.

Things worth knowing when the user asks:

- **It survives the session.** The panel is a detached process, so it stays up
  after Claude exits, and `off` closes it from anywhere.
- **`auto-on`** opens it with every new Claude session, via the plugin's
  SessionStart hook. `auto-off` stops that.
- **`install-app`** (macOS) puts a dockless *Claude Token Watch.app* in
  `~/Applications` so it can be launched from Spotlight with no terminal.
- **`snapshot`** prints the same live numbers as JSON — use it when you want the
  current totals yourself rather than showing the user a window. `--scope`
  picks the window: `5m`, `15m`, `1h`, `3h`, `24h`, `7d` or `all`. All but
  `all` are rolling, counted back from now, so they are the ones to use for
  "how much have I just spent". `all` is the total since the first recorded
  turn of any model or tool, and its `scope_label` names that date.
- **No Tkinter** means no panel. The script names the one install command for
  the platform; the reports are unaffected. Do not try to work around it with a
  browser page — the point of the panel is that it is not one.

## Running the report

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" token_report.py [options]
```

Python 3.8+, standard library only. No install step, no dependencies. `run.sh` finds a usable interpreter, so never hard-code `python3` in a command — on Windows that name often does not exist.

| Need | Command |
| --- | --- |
| Default view (30 days + dashboard) | `token_report.py` |
| Today only | `token_report.py --today` |
| A week, opened in the browser | `token_report.py --days 7 --open` |
| Everything ever recorded | `token_report.py --all` |
| A date range | `token_report.py --since 2026-09-01 --until 2026-09-07` |
| Turn-by-turn for one session | `token_report.py --session <id-prefix>` |
| One grouping, printed | `token_report.py --group-by model` |
| Machine-readable | `token_report.py --json` |
| Terminal only, no file written | `token_report.py --no-html` |
| Exclude subagent turns | `token_report.py --no-subagents` |

**Agents, loops, tools, harness:** every turn is attributed to its agent: the main agent, a subagent type such as `Explore`, or a Copilot mode (`--group-by agent`). It is also attributed to its **loop**: one user prompt and every model call it triggered, subagents included. The dashboard lists the heaviest loops (model calls, tool calls, subagents, run time, cost), every **tool called** including MCP tools, and **harness overhead**: setup tokens per session, the share of what's read that is re-sent history, and Copilot's split by prompt part. Agents the user built themselves are traced with `scripts/agent_tracer.py`, which logs to `~/.claude/token-tracker/agents/` and shows as `Custom agent · <app>`. Suggest it when the user asks about tracking their own agent.

`--group-by` accepts `day`, `model`, `app` (Claude Code / Cowork / Copilot / Custom agent), `surface`, `mode` (Copilot agent / ask / edit and chat location), `session`, `project`, `skill`, `plugin`, `mcp`.

The dashboard is written to `~/.claude/token-report.html` unless `--out` says otherwise. It is a single self-contained file with no external requests.

## Reporting well

Lead with the answer, not the method. Give the headline figure, name the dominant contributor, then stop. The script already prints the tables — do not transcribe them back.

When the user asks about a specific session, use `--session` and read the per-turn curve rather than the totals: the useful observation is usually that cost per turn climbs as the conversation grows, because each turn re-sends the whole history.

When they ask why something was expensive, look at the split:

- **Large cache reads, small everything else** — normal for a long conversation. The whole history is re-read each turn at 10% of input price. Cheap per token, but it accumulates.
- **Large cache writes** — a new or changed prompt prefix. Costs 1.25x (5-minute) or 2x (1-hour) input price. Expected at the start of a session or after tools change mid-session.
- **Large output** — Claude wrote a lot. Output is 5x input price, so this dominates fast. Includes thinking tokens.
- **Large fresh input** — a big paste, a long file read, or many tool results.
- **High turn count on a small task** — a tool loop. Often the real fix.

The "saved by caching" figure is what the cache reads would have cost at full input price minus what they actually cost. It is a genuine saving, not a discount on a bill the user was going to pay anyway.

## When the numbers look wrong

- **Nothing found** — the transcript directory is elsewhere. Pass `--root /path/to/projects`, or set `CLAUDE_CONFIG_DIR`. Both tools take `--root`.
- **The panel shows nothing** — no turn has landed since it opened and none exists in the window it holds. `python3 tokenwatch.py status` says what it is watching and how many transcripts it found.
- **A cost shows "—", or a total ends in "+"** — no live source publishes a rate for that model or tier yet (for example, a model released hours ago, or fast mode on a model that doesn't offer it). Its tokens still count. Never fill in a guessed cost. The cost appears once the price lists include it. To price it now, add its rates to `~/.claude/token-tracker/pricing.json`.
- **Every cost shows "—"** — the price lists have never been downloaded, because the machine is offline or `TOKEN_TRACKER_OFFLINE=1` is set. `--json` reports `pricing_fetched`.
- **Totals differ from Settings → Usage** — expected. The web and mobile surfaces are absent here, subscription billing is not per-token, and this tool prices at public list rates.
- **A number climbed while you watched** — transcripts are appended live, so a running session grows between runs.

Duplicate log entries for the same API request are charged once, keyed on `requestId`. Subagent turns are included by default and flagged; `--no-subagents` drops them.

## Keeping prices current

Nothing needs maintaining. Prices are downloaded once a day from Anthropic's pricing page and the LiteLLM list, and cached in `~/.claude/token-tracker/pricing-remote.json`. The plugin contains no price table, so a new model or a price change needs no code change. If a downloaded rate is wrong, the user can override it in `~/.claude/token-tracker/pricing.json`.
