# CardStar - compact layout for small or panning terminals.
# Copyright (C) 2026 TheBeardedHamm
# SPDX-License-Identifier: GPL-2.0-only
"""Gh0st OS's launcher terminal (gh0st-term) says it is 80x24, but its
display only shows about 50x12 and pans to follow the cursor. In compact
mode CardStar ignores the reported size, draws into a fixed 46x10 area at
the top left and keeps the (hidden) cursor there, so nothing is off-screen.

Chosen once at start, first match wins:
  1. --compact / --no-compact
  2. CARDSTAR_COMPACT=1 / 0
  3. "compact" in the settings file: "auto" (default), true or false
  4. auto: compact under gh0st-term, or if the terminal reports less than 80x24
"""
import curses
import os

ROWS, COLS = 10, 46             # the compact drawing area
FULL_ROWS, FULL_COLS = 24, 80   # smaller than this counts as a small terminal
GH0ST_TERM = "gh0st-term"
PROC = "/proc"

COMPACT = False                 # set once by choose_and_apply()
FORCE_QUIT_SHORT = "Esc/Home 5s=kill; TX may linger"


def parse_switch(value):
    """True / False from a flag-like value, or None for "auto" / unknown."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
    return None


def config_value(value):
    """What the settings file's "compact" key means: "auto", True or False.
    Anything unknown is treated as "auto" so an odd value never breaks loading."""
    switch = parse_switch(value)
    return "auto" if switch is None else switch


def _names(pid: int, proc: str):
    """(parent pid, names) for one process: its comm plus program names
    from the command line (a script's comm is its interpreter)."""
    with open(os.path.join(proc, str(pid), "stat"), "rb") as f:
        stat = f.read().decode("utf-8", "replace")
    # "pid (comm) state ppid ..." - comm may itself contain spaces or ")".
    left, right = stat.find("("), stat.rfind(")")
    names = {stat[left + 1:right]}
    ppid = int(stat[right + 2:].split()[1])
    try:
        with open(os.path.join(proc, str(pid), "cmdline"), "rb") as f:
            argv = f.read().split(b"\0")[:2]
        for arg in argv:
            base = os.path.basename(arg.decode("utf-8", "replace"))
            names.add(base[:-3] if base.endswith(".py") else base)
    except OSError:
        pass
    return ppid, names


def under_gh0st_term(pid=None, proc: str = PROC) -> bool:
    """Is any ancestor process gh0st-term? Walks /proc/<pid>/stat upwards.
    False wherever there is no /proc (and on any read problem)."""
    pid = os.getppid() if pid is None else pid
    for _ in range(64):                   # a sane limit on the chain
        if pid <= 0:
            return False
        try:
            ppid, names = _names(pid, proc)
        except (OSError, ValueError, IndexError):
            return False
        if GH0ST_TERM in names:
            return True
        if pid == 1 or ppid == pid:
            return False
        pid = ppid
    return False


def choose(flag, env, cfg_value, size, gh0st=None):
    """Decide compact mode. Returns (compact, reason).
    flag: True/False/None from the command line. env: CARDSTAR_COMPACT or None.
    cfg_value: the settings file's value. size: (rows, cols) the terminal reports.
    gh0st: result of under_gh0st_term() (looked up if None)."""
    if flag is not None:
        return flag, "command line"
    switch = parse_switch(env)
    if switch is not None:
        return switch, "CARDSTAR_COMPACT"
    switch = parse_switch(cfg_value)
    if switch is not None:
        return switch, "settings file"
    if gh0st is None:
        gh0st = under_gh0st_term()
    if gh0st:
        return True, "auto: gh0st-term"
    rows, cols = size
    if rows < FULL_ROWS or cols < FULL_COLS:
        return True, "auto: small terminal"
    return False, "auto"


def area(stdscr):
    """The window to draw in: the whole screen normally; in compact mode a
    fixed window at the top left (never bigger than the real terminal)."""
    if not COMPACT:
        return stdscr
    h, w = stdscr.getmaxyx()
    win = curses.newwin(min(ROWS, h), min(COLS, w), 0, 0)
    win.keypad(True)
    return win


def park(scr) -> None:
    """Compact mode: leave the cursor at the top left of the area, so a
    terminal that pans to follow the cursor keeps showing the area."""
    if COMPACT:
        try:
            scr.move(0, 0)
        except curses.error:
            pass


def force_quit_note(full: str) -> str:
    return FORCE_QUIT_SHORT if COMPACT else full
