---
description: Open or close the live token panel that sits on your desktop
argument-hint: "[on | off | status | auto-on | auto-off | install-app]"
allowed-tools: Bash(sh:*), Bash(python:*), Bash(python3:*), Bash(py:*)
---

Control the desktop panel. With no argument this toggles it.

```
!sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" tokenwatch.py $ARGUMENTS
```

If that failed with something like `sh: command not found`, this is native
Windows without Git for Windows, so Claude Code has no Bash tool. Rerun the
same thing with the interpreter directly — try these in order and stop at the
first that works:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/tokenwatch.py" $ARGUMENTS
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/tokenwatch.py" $ARGUMENTS
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tokenwatch.py" $ARGUMENTS
```

Mention once that installing Git for Windows removes the need for this.

Reply in one or two lines — the panel is visual, so it speaks for itself.

- Say whether it is now open or closed. Do not restate the script's output.
- The **first** time it opens for this user, add the controls once: drag it by
  its header, click the big number to switch between today / this session /
  the last 7 days, right-click for the menu, `✕` to close.
- If the script said Tkinter is unavailable, relay just the install line for
  their platform and mention that `/tokens` still reports in the terminal.
- If it reported no transcript directory, say that `--root` or
  `CLAUDE_CONFIG_DIR` points it at the right place.

`auto-on` makes the panel open with every new Claude session; `auto-off` stops
that. `install-app` is macOS only and installs a dockless *Claude Token Watch*
app into `~/Applications`, launchable from Spotlight without a terminal.

State the coverage limit the first time only, then leave it alone: the panel
sees the terminal CLI, the VS Code and JetBrains extensions, Cowork and the
Agent SDK, because those write transcripts to disk. It cannot see claude.ai in
the browser, the Claude desktop chat app, or mobile — those write no transcript
and expose no per-conversation usage API. Cost is an estimate at list API
prices, so on a subscription it reads as relative weight, not as a bill.
