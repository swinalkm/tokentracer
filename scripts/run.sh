#!/bin/sh
# Find a usable Python 3 and run one of this plugin's scripts with it.
#
# Hard-coding `python3` is not portable: the python.org installer on Windows
# provides only `python` and the `py` launcher, so a Windows user would see
# nothing but "python3: command not found" — and the whole point of this
# plugin is that installing it is the only step.
#
# Hooks can also run with a trimmed PATH, so the usual absolute locations are
# tried as well as the bare names.
#
#   Usage: sh run.sh <script.py> [args...]

set -u
set -f          # no globbing while splitting the candidate list

if [ "$#" -eq 0 ]; then
    echo "run.sh: expected a script name" >&2
    exit 64
fi

script_name=$1
shift

# Resolved with parameter expansion rather than `dirname`: a hook can run
# with almost no PATH, and this must work before any external command does.
here=${0%/*}
[ "$here" = "$0" ] && here="."
script="$here/$script_name"

if [ ! -f "$script" ]; then
    echo "run.sh: no such script: $script" >&2
    exit 66
fi

CANDIDATES="python3 python
    python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8
    /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3"

PYBIN=""
PYARG=""

# Sets PYBIN/PYARG to the first candidate that satisfies $1, else returns 1.
find_python() {
    _probe=$1
    for _cand in $CANDIDATES; do
        if command -v "$_cand" >/dev/null 2>&1 &&
           "$_cand" -c "$_probe" >/dev/null 2>&1; then
            PYBIN=$_cand
            PYARG=""
            return 0
        fi
    done
    # The Windows launcher knows where Python is even when PATH does not.
    if command -v py >/dev/null 2>&1 && py -3 -c "$_probe" >/dev/null 2>&1; then
        PYBIN="py"
        PYARG="-3"
        return 0
    fi
    return 1
}

WANT_38='import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 8) else 1)'
WANT_TK='import sys, tkinter; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)'

# The panel needs Tkinter, so prefer an interpreter that has it. Falling back
# to any Python 3 is deliberate: tokenwatch.py then prints the one install
# command for the platform, which beats a generic "no Python" message.
case "$script_name" in
    tokenwatch.py) find_python "$WANT_TK" || find_python "$WANT_38" ;;
    *)             find_python "$WANT_38" ;;
esac

if [ -z "$PYBIN" ]; then
    # echo, not cat or printf: a hook may run with almost no PATH, and this
    # message is the one thing that must survive to explain the problem.
    echo "Python 3.8 or newer is required, and none could be found." >&2
    echo "" >&2
    echo "  macOS    It ships as /usr/bin/python3. If it is missing, run:" >&2
    echo "             xcode-select --install" >&2
    echo "  Windows  Install \"Python 3\" from the Microsoft Store, or from" >&2
    echo "           python.org with \"Add python.exe to PATH\" ticked." >&2
    echo "  Linux    sudo apt install python3 python3-tk      (Debian/Ubuntu)" >&2
    echo "           sudo dnf install python3 python3-tkinter (Fedora)" >&2
    exit 127
fi

if [ -n "$PYARG" ]; then
    exec "$PYBIN" "$PYARG" "$script" "$@"
fi
exec "$PYBIN" "$script" "$@"
