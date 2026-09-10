# claude-token-tracker

See exactly how many tokens each Claude turn used, and what it cost.

A small panel that sits on your desktop and counts every turn as it happens, plus reports for when you want the detail.

Claude Code, the editor extensions, and Cowork all write a transcript for every session to `~/.claude/projects/`. Each assistant turn in those files carries the API's own `usage` record — input, output, cache reads, cache writes, thinking tokens, model, and web-search count. This plugin tails those files, prices each turn, and shows you the running total.

No dependencies. No network calls. Nothing leaves your machine.

```
  Claude token usage — Last 30 days
  ----------------------------------------------------
  Turns              207   across 10 sessions
  Tokens billed      22.78M
    fresh input      42.6k
    cache reads      20.19M
    cache writes     2.25M
    output           302.4k   (87.3k thinking)
  Estimated cost     $20.44
  Saved by caching   $51.49
```

## The live panel

```
/tokens-watch
```

A borderless window, about 320px wide, that stays on top of your other windows
and updates within a second of each turn landing — including the turns Claude
takes on its own in the middle of a long task, and subagent turns.

```
   ┌─ Claude tokens ───────────── – ✕ ┐
   │  ● live · 4s ago · VS Code · cai │
   │                                  │
   │   3.63M                   $3.29  │
   │   LAST HOUR         tokens · cost│
   │                                  │
   │  5m  15m  1h  3h  24h day sess all│
   │                                  │
   │   last turn  +135.6k · $0.13 ·   │
   │   Opus 5                         │
   │   ▁▂▃▅▂▇▃▂▁▃▅▇▂▁▃▂▅▃▁▂▇▃▂▁▃▅▂▁█  │
   │   cost per turn · last 30        │
   │                                  │
   │   Opus 5           3.63M  $3.29  │
   │   Sonnet 5          265k  $0.74  │
   │                                  │
   │   cache 3.48M · write 81.3k ·    │
   │   out 30.0k · in 60              │
   │   30 turns · 22m · saved $15.66  │
   └──────────────────────────────────┘
```

It is a real desktop window, not a browser tab — Tkinter, which ships with
Python, so there is nothing to install and it works the same on macOS, Windows
and Linux.

| Control | Does |
| --- | --- |
| The chip row | Pick the window: `5m` `15m` `1h` `3h` `24h` `day` `sess` `all` |
| Drag the header | Move it. The position is remembered. |
| Click the big number | Step through the windows in order |
| `–` | Collapse to a compact bar; `+` expands it again |
| `✕` | Close |
| Right-click | Every window by name, always-on-top, copy summary, full report, quit |

The rolling windows — `5m` through `24h` — are counted back from this instant
and recomputed every second, so they fall as work stops as well as rising as
it happens. `day` is the calendar day, `sess` is the session in use, and `all`
is everything the panel holds in memory (7 days by default, `--days` changes
it). Whichever you pick is remembered for next time.

```
/tokens-watch            # toggle it
/tokens-watch on         # or off, status
/tokens-watch auto-on    # open it with every new Claude session
/tokens-watch auto-off
/tokens-watch install-app   # macOS: see below
```

### Launching it without Claude (macOS)

```
/tokens-watch install-app
```

installs **Claude Token Watch.app** into `~/Applications`. Launch it from
Spotlight and the panel appears with no terminal and no Dock icon — it is
registered as an accessory app, so it stays out of your way. Close it with the
`✕` on the panel.

### Direct CLI

```bash
sh scripts/run.sh tokenwatch.py           # portable: finds an interpreter
python3 scripts/tokenwatch.py             # or call it directly — same thing
python3 scripts/tokenwatch.py on
python3 scripts/tokenwatch.py off
python3 scripts/tokenwatch.py status
python3 scripts/tokenwatch.py snapshot    # the live numbers as JSON
python3 scripts/tokenwatch.py --run       # foreground, for debugging
```

| Flag | Effect |
| --- | --- |
| `--scope 5m\|15m\|1h\|3h\|24h\|today\|session\|window` | Which window to headline |
| `--interval SECONDS` | How often to check for new turns (default 1.0) |
| `--opacity 0-1` | Panel opacity (default 0.96) |
| `--decorated` | Keep the normal window frame |
| `--days N` | Days of history held in memory (default 7) |
| `--no-subagents` | Exclude subagent turns |
| `--root DIR` | Watch a different transcript directory (repeatable) |

Reading is incremental: each transcript's byte offset is remembered and only
whole lines are parsed, so a file being appended to mid-write is picked up on
the next pass instead of producing a half-read turn. A request logged twice is
still counted once.

## What it covers, and what it can't

**Exact, from the transcripts:** Claude Code in the terminal · VS Code and JetBrains extensions · Cowork · Agent SDK · scheduled tasks · subagents. These numbers are the API's own counts, not estimates.

**Not covered:** claude.ai in the browser, the desktop chat window, and the mobile apps. Those keep no transcript on disk and expose no per-conversation usage API, so no tool can read them — this one included. Use **Settings → Usage** for those.

That gap is real and this plugin states it on every report rather than quietly under-counting.

## Install

Inside Claude Code, on every platform, it is these two lines:

```
/plugin marketplace add swinalkamble/claude-token-tracker
/plugin install claude-token-tracker@token-tracker
```

Then `/tokens-watch` and the panel appears. Everything below is the one-off
platform setup those two lines assume — on a Mac you almost certainly have it
already.

### macOS

Python ships with macOS, so there is usually nothing to do. Confirm it in
Terminal:

```bash
python3 --version
```

If that prints `Python 3.9` or newer, you are done — go run the two `/plugin`
lines above. If it says *command not found*, install Apple's developer tools
and check again:

```bash
xcode-select --install
python3 --version
```

Optional extras, run inside Claude Code after installing:

```
/tokens-watch install-app    # a dockless app in ~/Applications, launchable from Spotlight
/tokens-watch auto-on        # open the panel with every new Claude session
```

### Windows

Two one-off installs in **PowerShell**:

```powershell
winget install --id Python.Python.3.13 --exact
winget install --id Git.Git --exact
```

- **Python** is what the plugin runs on, and it bundles Tkinter, which the
  panel needs. If you prefer, install "Python 3" from the Microsoft Store
  instead, or use python.org and tick *Add python.exe to PATH*.
- **Git for Windows** gives Claude Code its Bash tool. Claude Code's own setup
  guide recommends it on native Windows, and without it `/tokens-watch auto-on`
  cannot start the panel automatically. `/tokens-watch` itself still works
  either way.

Close PowerShell, open a new one, and confirm:

```powershell
python --version
```

Then run the two `/plugin` lines above inside Claude Code.

> On WSL, ignore all of the above and follow the Linux steps inside your WSL
> terminal instead.

### Linux

```bash
sudo apt install python3 python3-tk       # Debian, Ubuntu
sudo dnf install python3 python3-tkinter  # Fedora, RHEL
```

`python3-tk` is the part that matters: without it the reports still work but
the panel cannot open. Then run the two `/plugin` lines above.

### Updating and removing

```
/plugin update claude-token-tracker
/plugin uninstall claude-token-tracker
```

## Use

The same commands on macOS, Windows and Linux:

| Command | What it does |
| --- | --- |
| `/tokens-watch` | Open the live desktop panel, or close it if it is up |
| `/tokens-watch on` / `off` | Open or close it explicitly |
| `/tokens-watch status` | Is it running, and which transcripts is it watching |
| `/tokens-watch auto-on` / `auto-off` | Open it automatically with every new session |
| `/tokens-watch install-app` | macOS only — a Spotlight-launchable app |
| `/tokens` | Last 30 days: a summary plus the HTML dashboard |
| `/tokens --today` | Just today |
| `/tokens --days 7 --open` | A week, opened in the browser |
| `/tokens --all` | Everything ever recorded |
| `/tokens --group-by model` | One breakdown, printed |

Or just ask in plain language — the bundled skill picks it up:

> how many tokens did I use today?
> why was that session so expensive?
> which project is costing me the most?
> is caching actually helping me?

### Direct CLI

The script stands alone; you don't need Claude to run it.

```bash
python3 scripts/token_report.py --days 7 --open
python3 scripts/token_report.py --session 4cf91daf        # turn-by-turn
python3 scripts/token_report.py --json | jq .totals
```

| Flag | Effect |
| --- | --- |
| `--days N` | Look back N days (default 30) |
| `--today` / `--all` | Today only / the entire history |
| `--since` `--until` | Explicit `YYYY-MM-DD` range |
| `--session ID` | Turn-by-turn table for one session (id prefix is enough) |
| `--group-by FIELD` | `day`, `model`, `surface`, `session`, `project`, `skill`, `plugin`, `mcp` |
| `--json` | Machine-readable output |
| `--out PATH` | Where to write the dashboard (default `~/.claude/token-report.html`) |
| `--no-html` | Terminal only |
| `--open` | Open the dashboard when done |
| `--no-subagents` | Exclude subagent (sidechain) turns |
| `--root DIR` | Scan a different transcript directory (repeatable) |
| `--utc` | Bucket days by UTC rather than local time |

## The dashboard

A single self-contained HTML file — no CDN, no scripts, no tracking. Light and dark mode. It shows:

- cost and tokens per day, with idle days left visible as gaps
- totals, turn and session counts, and what caching saved you
- breakdowns by model, surface, project, and heaviest sessions
- breakdowns by **skill, plugin, and MCP server**, so you can see which tools are actually expensive
- every caveat printed on the page, not buried in a footnote

## How the accounting works

Each turn is priced from `scripts/pricing.json` at published list rates:

| Component | Rate |
| --- | --- |
| Fresh input | base input price |
| Cache read | 0.1x input (0.025x on Fable 5.1 / Mythos 5.1) |
| Cache write, 5-minute | 1.25x input |
| Cache write, 1-hour | 2x input |
| Output (thinking included) | 5x input, per model |
| Web search | $10 per 1,000 searches |

Cache writes are priced at their real TTL where the transcript records it, rather than assuming one rate for both. Batch tier gets the 50% discount; US-pinned inference gets the 1.1x multiplier; Opus fast mode gets its premium rates.

A single API request logged more than once — from a resume, a re-render, or a sidechain echo — is charged once, keyed on `requestId`. A model with no entry in `pricing.json` has its tokens counted and its cost reported as zero, flagged clearly. Guessing a rate would be worse than admitting the gap.

### Cost is an estimate, deliberately labelled as one

On Pro, Max, Team, or Enterprise you are not billed per token, so treat the figure as **relative weight** — useful for "this session cost 8x that one", not for reconciling an invoice. Only on API/Console billing does it approximate real spend. Your account's authority is always Settings → Usage or the Console.

### Keeping prices current

`pricing.json` carries the date it was last checked. Prices change; refresh it from the [pricing page](https://platform.claude.com/docs/en/about-claude/pricing) and bump `_checked`. The script reads the file at runtime, so no code change is needed.

## Reading your own numbers

A few things surprise people the first time:

- **Setup usually dominates a short session.** The system prompt and tool definitions are re-sent every turn. Over few turns that fixed cost is most of the bill; over many turns it amortises.
- **Cache reads are large but cheap.** Millions of cache-read tokens at 10% of input price often cost less than a few thousand output tokens.
- **Output is the expensive part.** It's 5x input, so a long answer outweighs a long prompt.
- **Cost per turn climbs through a conversation** because the whole history is re-sent each time. `--session` shows that curve directly.

## Privacy

Both the panel and the reports read only the `usage` and metadata fields — token counts, model ids, timestamps, session ids, working-directory names. It never reads message content, and it makes no network requests of any kind. The dashboard is a local file.

## Requirements

Installing the plugin is the only step. There is nothing to clone, build, or
configure, and no file to edit.

Python 3.8 or newer for the reports, 3.9 or newer for the panel — standard
library only, on macOS, Linux, or Windows. You almost certainly have it:

| Platform | Where Python comes from | Tkinter? |
| --- | --- | --- |
| macOS | `/usr/bin/python3` with the Xcode command line tools | yes (Tk 8.5) |
| Windows | Microsoft Store or python.org installer | yes |
| Linux | the distribution's `python3` | needs `python3-tk` on Debian/Ubuntu |

The interpreter is found by [`scripts/run.sh`](scripts/run.sh), which every
command and hook goes through. It tries `python3`, `python`, versioned names,
the usual absolute locations, and the Windows `py -3` launcher, rejecting any
that is too old — so the plugin works where `python3` is not a valid command
name, which includes a default python.org install on Windows. It needs no
external tools itself and works with an empty `PATH`.

Two things it cannot do for you:

- **Tkinter on Debian/Ubuntu** is a separate package. Without it there is no
  panel, and `/tokens-watch` tells you exactly which command installs it. The
  reports work regardless.
- **No Python at all** — it prints the one install line for your platform.

## Licence

MIT — see [LICENSE](LICENSE).
