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
  Cost               $20.44
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
| The chip row | Pick the window: `5m` `15m` `1h` `3h` `24h` `7d`. `all ↗` opens the all-time dashboard in your browser |
| Drag the header | Move it. The position is remembered. |
| Click the big number | Step through the windows in order |
| `–` | Collapse to a compact bar; `+` expands it again |
| `✕` | Close |
| Right-click | Every window by name, always-on-top, copy summary, full report, quit |

The windows `5m` through `7d` are rolling: they are counted back from this
instant and recomputed every second, so they fall as work stops as well as
rising as it happens. Whichever window you pick is remembered for next time.
The default is `24h`.

`all ↗` keeps the panel small. Instead of filling it with your whole history,
it opens the all-time dashboard in your default browser. The dashboard shows
your total since day one, for every model and tool on record: cost, tokens,
and turns for each platform and each model. It also has a calendar. Each day
shows its token total, an hour-by-hour bar graph of when the tokens were used,
and shading for how heavy the day was compared with your other days. Hover
over a day, or tab to it, for its breakdown by tool and model. The right-click
menu has the same option. `--snapshot --scope all` gives the all-time totals as
JSON.

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
| `--scope 5m\|15m\|1h\|3h\|24h\|7d\|all` | Which window to headline |
| `--interval SECONDS` | How often to check for new turns (default 1.0) |
| `--opacity 0-1` | Panel opacity (default 0.96) |
| `--decorated` | Keep the normal window frame |
| `--days N` | Days of history held in memory (default: all of it) |
| `--no-subagents` | Exclude subagent turns |
| `--no-copilot` | Leave out GitHub Copilot Chat |
| `--root DIR` | Watch a different transcript directory (repeatable) |

Reading is incremental: each transcript's byte offset is remembered and only
whole lines are parsed, so a file being appended to mid-write is picked up on
the next pass instead of producing a half-read turn. A request logged twice is
still counted once.

## What it covers, and what it can't

**Exact, from the transcripts:** Claude Code in the terminal · VS Code and JetBrains extensions · Cowork · Agent SDK · scheduled tasks · subagents. These numbers are the API's own counts, not estimates.

**Not covered:** claude.ai in the browser, the desktop chat window, and the mobile apps. Those keep no transcript on disk and expose no per-conversation usage API, so no tool can read them — this one included. Use **Settings → Usage** for those.

That gap is real and this plugin states it on every report rather than quietly under-counting.

### Agents, subagents, loops, tools and the harness

Each turn is attributed to everything that was involved in it:

| What | How it's traced | Where to see it |
| --- | --- | --- |
| **Main agent and subagents** | Claude Code logs each subagent in `<session>/subagents/` together with its type (`Explore`, `Plan`, your own `.claude/agents/*`). Copilot marks its subagent requests. | "Agents and subagents" on the dashboard, and `--group-by agent` (`Claude Code · main`, `Claude Code · Explore`, `Copilot · agent`, …) |
| **Agent loops** | A loop is one prompt you sent and every model call it set off until the agent stopped. Subagents are counted in the loop of the prompt that started them. | "Agent loops": model calls, tool calls, subagents, run time, tokens and cost for each loop |
| **Tools** | Every tool name an agent called, including MCP tools (`mcp__server__tool`). The MCP server is also worked out from the tool name. | "Tools called", `--group-by mcp` |
| **Skills and plugins** | Claude Code records these per turn. | `--group-by skill`, `--group-by plugin` |
| **Harness overhead** | Setup per session (the first call's prompt: system prompt, tool and MCP definitions, skills, CLAUDE.md, memory), and the share of what's read that is re-sent history. Copilot's own split by prompt part. | "Harness overhead" |
| **Your own agents** | `scripts/agent_tracer.py`, below | All of the above, as `Custom agent · <app>` |

#### Tracing an agent you built yourself

An agent written against the API directly (your own loop, the Anthropic or OpenAI SDK, LangChain, …) writes no logs, so nothing can read it. Copy [`scripts/agent_tracer.py`](scripts/agent_tracer.py) into your project. It's a single file that needs only the standard library:

```python
from agent_tracer import TokenTracer

tracer = TokenTracer(app="research-bot")
client = tracer.instrument(anthropic.Anthropic())   # logs every messages.create()

with tracer.loop():                                  # one task or prompt
    client.messages.create(...)                      # step 1, step 2, ...
    with tracer.subagent("summariser"):              # delegated work
        client.messages.create(...)                  # counted in the same loop
```

For a streamed reply, or a client it doesn't wrap, call `tracer.record(final_message)`. You can also call `tracer.record(usage={...}, model="...")` with any usage dict. It understands Anthropic and OpenAI-style responses. Each call is written as one line to `~/.claude/token-tracker/agents/<app>/<session>.jsonl`, in the same format Claude Code uses. Only the model, token counts, tool names and agent/loop IDs are written, never prompts, replies or tool inputs. The tracker reads that folder automatically and prices the calls at live list prices.

**Can't be traced:** hooks (they don't record token usage), Claude Managed Agents (they run on Anthropic's servers; use the Console), and anything else that writes no usage log on your machine.

### GitHub Copilot

Copilot Chat in **VS Code** and **VS Code Insiders** is included too, whichever model you pick, Claude included. It is read from the chat logs Copilot keeps on your machine (`workspaceStorage/*/chatSessions/`). Copilot rows are labelled so they never merge with Claude Code's:

| Shown as | Example |
| --- | --- |
| Model | `Copilot · Sonnet 4.5`, `Copilot · Opus 4.5`, `Copilot · gpt-5.6-luna`. When you pick "Auto", the model that actually answered is shown. |
| Tool | `Copilot`, next to `Claude Code`, `Cowork`, `Agent SDK` |
| Surface | `Copilot · VS Code`, next to `Claude Code · VS Code`, `Claude Code · Terminal`, … |
| Mode | `agent mode · panel`, `ask mode · editor`, `custom agent mode · panel` (`--group-by mode`) |
| Project | the workspace folder the chat ran in |

**Cost** for Copilot comes from what Copilot itself recorded charging, in GitHub **AI credits**, converted to dollars at GitHub's published rate. [GitHub's billing docs](https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-individuals) state "1 AI credit = $0.01 USD", and that rate is downloaded daily along with the other prices. For example, 139.83 AI credits × $0.01 = $1.40. Copilot doesn't log each model call's prompt or any cache figures, so its cost can't be recalculated from its tokens. The credits it recorded are the true charge.

**Every cost is in US dollars**, and totals carry platform tags, for example `Total cost (USD) $24.53` with `Claude Code $23.13 · Copilot $1.40`. The dashboard ends with a **How cost is calculated** section that shows each platform's formula with your own numbers plugged in.

**Token counts** for Copilot are the final model call's prompt, plus all the output and thinking across the request. In a multi-step agent run, the earlier calls' prompts aren't logged, so treat Copilot input as a floor.

**Not covered:** Copilot in JetBrains, Visual Studio, Xcode, and the Copilot CLI. None of these keep token counts on disk. `--no-copilot` leaves Copilot out entirely.

## Install

Inside Claude Code, on every platform, it is these two lines:

```
/plugin marketplace add https://github.com/swinalkm/tokentracer.git
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
| `--group-by FIELD` | `day`, `model`, `app`, `agent`, `surface`, `mode`, `session`, `project`, `skill`, `plugin`, `mcp` |
| `--json` | Machine-readable output |
| `--out PATH` | Where to write the dashboard (default `~/.claude/token-report.html`) |
| `--no-html` | Terminal only |
| `--open` | Open the dashboard when done |
| `--no-subagents` | Exclude subagent (sidechain) turns |
| `--no-copilot` | Leave out GitHub Copilot Chat |
| `--root DIR` | Scan a different transcript directory (repeatable) |
| `--utc` | Bucket days by UTC rather than local time |

## The dashboard

A single self-contained HTML file: no CDN, no tracking, and only a small inline script for the calendar's hover cards. Light and dark mode. It shows:

- a **calendar**, one month at a time: it opens on the latest month, and ‹ › (or the arrow keys) step back and forward. Each day shows its tokens, an hour-by-hour bar graph, and shading for how heavy the day was. Hover over a day for its tools, models, cost and busiest hour. **Click a day** to open a side panel listing every session and chat from that day, with its title (sessions you named are marked **Named**), app, project, times, model calls, tokens, cost, loops and subagents, plus how to continue it:
  - **Claude Code** (terminal, VS Code, desktop app): a copyable `cd <folder> && claude --resume <session id>` that reopens the conversation where it left off, and a link to open the folder in VS Code
  - **Copilot**: a link to open the folder in VS Code; then pick the chat from Copilot's chat history (VS Code has no link straight to a chat)
  - Pinned or starred chats are not shown: the Claude apps keep pins in their own storage or on the server, not in any file this tool can read
- totals **by platform and model** together (for example `Copilot · VS Code` with `Copilot · Sonnet 4.5`), and by tool
- totals, turn and session counts, and what caching saved you
- breakdowns by model, surface, project, and heaviest sessions
- breakdowns by **skill, plugin, and MCP server**, so you can see which tools are actually expensive
- every caveat printed on the page, not buried in a footnote

## How the accounting works

Cost is calculated from each turn's own token counts and rates downloaded live. The plugin contains no price table, and it never guesses a rate.

```
cost = fresh input × input rate
     + cache reads × cache-read rate
     + 5-minute cache writes × 5m write rate
     + 1-hour cache writes × 1h write rate
     + output (thinking included) × output rate
     + web searches × per-search rate
```

Each part uses the rate for the tier the transcript records for that turn: standard, fast mode, or the Batch API. If the turn ran with US-only inference, its multiplier is applied. On Haiku 5.5, a prompt over 100,000 tokens is charged the higher long-context rates. Cache writes are charged at their actual 5-minute or 1-hour rate wherever the transcript says which was used.

**If any rate a turn needs is unknown, that turn shows no cost.** It appears as "—", rather than as $0.00 or a guess. A total that leaves out some unpriced turns ends in "+" (for example `$7.15+`), so it reads as "at least". Tokens are always counted, for every model a transcript names, including ones released after you installed the plugin.

A single API request logged more than once — from a resume, a re-render, or a sidechain echo — is charged once, keyed on `requestId`. Each model is labelled from its id (`claude-opus-5-5` shows as "Opus 5.5"), so a new release is never folded into an older one.

### Where prices come from

When Anthropic releases a model or changes a price, nothing in the plugin needs to change. Each rate is looked up in this order, and the first match wins:

1. **Your own rates, optional.** `~/.claude/token-tracker/pricing.json` lives outside the plugin, so updates never touch it. Use it to price a private or custom model, or to correct a rate. Rates are in USD per million tokens:
   ```json
   {"models": {"my-model": {"input": 1.0, "output": 5.0,
                            "cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1}}}
   ```
2. **Anthropic's [pricing page](https://platform.claude.com/docs/en/about-claude/pricing).** It is read in its Markdown form, which provides the model rates, fast-mode and batch rates, the US-only inference multiplier, web search pricing, and Haiku 5.5's long-context rates. Columns are found by their headings, so a reordered or widened table still reads correctly.
3. **The public [LiteLLM price list](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json).** It covers other providers' models, and any Claude model the pricing page doesn't list yet.

Both live sources are downloaded at most once a day and cached in `~/.claude/token-tracker/pricing-remote.json`. Offline, the last rates fetched are used. If nothing has ever been fetched, tokens are shown without cost. A panel that's already open notices a model it can't price and refreshes the lists in the background, at most once an hour.

| Variable | Effect |
| --- | --- |
| `TOKEN_TRACKER_OFFLINE=1` | Never download prices. Only your own rates and the last cached download are used. |
| `TOKEN_TRACKER_PRICING_URL=https://…` | Use your own file in place of the LiteLLM list, in either format. |

### Cost is list price, not your bill

On Pro, Max, Team, or Enterprise you are not billed per token, so treat the figure as **relative weight** — useful for "this session cost 8x that one", not for reconciling an invoice. Only on API/Console billing does it approximate real spend. Your account's authority is always Settings → Usage or the Console.

## Reading your own numbers

A few things surprise people the first time:

- **Setup usually dominates a short session.** The system prompt and tool definitions are re-sent every turn. Over few turns that fixed cost is most of the bill; over many turns it amortises.
- **Cache reads are large but cheap.** Millions of cache-read tokens at 10% of input price often cost less than a few thousand output tokens.
- **Output is the expensive part.** It's 5x input, so a long answer outweighs a long prompt.
- **Cost per turn climbs through a conversation** because the whole history is re-sent each time. `--session` shows that curve directly.

## Privacy

Both the panel and the reports read only the `usage` and metadata fields — token counts, model ids, timestamps, session ids, working-directory names, and for Copilot its token, credit, mode and model fields. To follow agents and loops it also reads ids (`promptId`, `parentUuid`, agent ids), subagent `.meta.json` files, content-block *types*, and the *names* of the tools called. It also reads **session titles** (Claude Code's generated title or the name you gave a session, and Copilot's chat title) so the calendar's side panel can tell sessions apart. Titles are summaries of a conversation, so the dashboard file contains them. It stays on your machine. It never reads message text or tool inputs, from any tool. The only network requests are a once-a-day download of two public price lists, Anthropic's pricing page and the LiteLLM list. Each is a plain GET for a static file, and nothing about you or your usage is sent. Set `TOKEN_TRACKER_OFFLINE=1` to turn it off. The dashboard is a local file.

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
