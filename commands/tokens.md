---
description: Show token usage and cost across your local Claude sessions, and open a dashboard
argument-hint: "[--days N | --today | --all] [--open] [--group-by day|model|surface|session|project|skill|plugin|mcp]"
allowed-tools: Bash(sh:*), Bash(python:*), Bash(python3:*), Bash(py:*)
---

Run the tracker and report what it says.

```
!sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" token_report.py $ARGUMENTS
```

If that failed with something like `sh: command not found`, this is native
Windows without Git for Windows, so Claude Code has no Bash tool. Rerun the
same thing with the interpreter directly — try these in order and stop at the
first that works:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/token_report.py" $ARGUMENTS
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/token_report.py" $ARGUMENTS
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/token_report.py" $ARGUMENTS
```

Mention once that installing Git for Windows removes the need for this.

Then, in a short reply:

1. Lead with the headline: total cost, total tokens, and the window covered.
2. Name the biggest contributor — whichever of model, surface, or project dominates — and say by how much.
3. Point out anything genuinely notable: an unusually expensive day, a large cache-savings figure, a session far heavier than the rest. Skip this if nothing stands out; do not manufacture insight.
4. If any turns were unpriced, say which models and that their tokens are counted but their cost is not.
5. Mention the dashboard path once, at the end.

Keep it to a few lines. Do not restate the whole table the script already printed.

Always preserve two caveats when reporting, because leaving them out makes the numbers misleading:

- **claude.ai web, the desktop chat window, and the mobile apps are not included.** They write no transcript to disk and expose no per-conversation usage API. Direct the user to Settings → Usage for those.
- **Cost is an estimate at list API prices.** On a subscription plan the user is not billed per token, so the figure reads as relative weight, not as an invoice.
