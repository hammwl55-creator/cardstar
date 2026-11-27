# CardStar - settings file and setup screen.
# Copyright (C) 2026 TheBeardedHamm
# SPDX-License-Identifier: GPL-2.0-only
"""Settings live in ~/.config/cardstar/config (folder 700, file 600).
Nothing here prints or logs any setting."""
import asyncio
import curses
import json
import os
import re
import tempfile

import cardstar_layout

CONFIG_DIR = os.path.expanduser("~/.config/cardstar")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config")
DEFAULT_PORT = 4569   # the standard IAX2 port; used if the port is left blank
# Software microphone gain (a multiplier on what the mic records).
DEFAULT_MIC_GAIN = 4.0
MIN_MIC_GAIN, MAX_MIC_GAIN = 1.0, 10.0

# name, label, hint, longest allowed
FIELDS = [
    ("host", "Server", "Name or IP address of your server", 253),
    ("port", "Port", "Blank = 4569", 5),
    ("username", "Username", "Your IAX2 login name", 64),
    ("secret", "Secret", "Saved as plain text in a private file", 128),
    ("node", "Node", "Node number to connect to", 20),
    ("caller_id", "Caller ID", "Optional - may be left blank", 64),
    ("mic_gain", "Mic gain", "1 to 10, blank = 4 (normal)", 4),
]
FORCE_QUIT_NOTE = "Esc/Home held 5s kills; TX may linger"
SHOW_HIDE_KEYS = (curses.KEY_LEFT, curses.KEY_RIGHT, "\t")   # on the secret field


def check(name: str, value: str) -> str:
    """Return a short complaint about a value, or "" if it is fine."""
    if name == "host":
        if not value or any(c.isspace() for c in value):
            return "Server is needed (no spaces)"
    elif name == "port":
        if value and not (value.isascii() and value.isdigit() and 0 < int(value) < 65536):
            return "Port must be 1-65535"
    elif name == "username":
        if not value.strip():
            return "Username is needed"
    elif name == "secret":
        if not value:
            return "Secret is needed"
    elif name == "node":
        if not (value.isascii() and value.isdigit()):
            return "Node number is needed (digits only)"
    elif name == "mic_gain":
        if value and not (re.fullmatch(r"[0-9]+(\.[0-9]*)?", value)
                          and MIN_MIC_GAIN <= float(value) <= MAX_MIC_GAIN):
            return "Mic gain must be 1 to 10"
    return ""


def clamp_gain(value) -> float:
    """A usable gain from whatever is in the file (missing = the default)."""
    try:
        gain = float(value)
    except (TypeError, ValueError):
        return DEFAULT_MIC_GAIN
    if gain != gain:                       # "nan"
        return DEFAULT_MIC_GAIN
    return min(MAX_MIC_GAIN, max(MIN_MIC_GAIN, gain))


def make_private() -> None:
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)


def load():
    """The saved settings, or None if there are none or they are damaged."""
    try:
        make_private()
        os.chmod(CONFIG_FILE, 0o600)
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        cfg = {name: data.get(name, "") for name, *_ in FIELDS}
        cfg["port"] = str(cfg["port"])
        gain = clamp_gain(data.get("mic_gain"))
        cfg["mic_gain"] = ""               # checked separately, just above
        if not all(isinstance(v, str) for v in cfg.values()):
            return None
        if any(check(name, value) for name, value in cfg.items()):
            return None
        cfg["port"] = int(cfg["port"] or DEFAULT_PORT)
        cfg["mic_gain"] = gain
        cfg["compact"] = cardstar_layout.config_value(data.get("compact", "auto"))
        return cfg
    except (OSError, ValueError, AttributeError):
        return None


def read_compact():
    """Just the "compact" setting ("auto", True or False), even when the rest
    of the file is missing or incomplete (as on the first-run screen)."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return cardstar_layout.config_value(data.get("compact", "auto"))
    except (OSError, ValueError, AttributeError):
        return "auto"


def save(cfg: dict) -> None:
    make_private()
    if "compact" not in cfg:               # never drop the setting
        cfg = dict(cfg, compact=read_compact())
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config-")  # mode 600
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def footer_keys(editing: bool) -> str:
    # First run: Esc leaves the app. Editing: Esc goes back to the main screen.
    return f"Enter=next Up/Down=move Esc={'back' if editing else 'quit'}"


def _draw(scr, values, at, message, editing, reveal) -> None:
    scr.erase()
    h, w = scr.getmaxyx()

    def put(y, x, text, attr=0):
        if 0 <= y < h:
            try:
                scr.addnstr(y, x, text, max(0, w - x - 1), attr)
            except curses.error:
                pass

    name, _label, hint, _longest = FIELDS[at]
    if name == "secret":
        value = values["secret"]
        count = f"{len(value)} characters"
        if reveal:                          # the whole secret, wrapped
            put(0, 0, f"Secret shown - {count}", curses.A_BOLD)
            if editing and not value:
                put(1, 0, "(saved secret is never shown)")
            else:
                text, wide = value + "_", max(1, w - 1)
                for row, start in enumerate(range(0, len(text), wide)):
                    if 1 + row < h - 2:
                        put(1 + row, 0, text[start:start + wide])
            put(h - 2, 0, message or "Left/Right = hide", curses.A_BOLD if message else 0)
            put(h - 1, 0, footer_keys(editing), curses.A_DIM)
            cardstar_layout.park(scr)
            scr.refresh()
            return
        if editing and not value:
            hint = "Blank keeps it. Left/Right = show"
        else:
            hint = f"{count}. Left/Right = show"

    footer = []
    if h >= 9:
        footer.append((message or hint, curses.A_BOLD if message else 0))
    footer.append((message if message and h < 9
                   else cardstar_layout.force_quit_note(FORCE_QUIT_NOTE), 0))
    footer.append((footer_keys(editing), curses.A_DIM))
    rows = max(1, h - len(footer))          # fields visible; scrolls if short
    first = max(0, min(at - rows + 1, len(FIELDS) - rows))

    y = 0
    if rows > len(FIELDS):
        put(y, 0, "CardStar setup", curses.A_BOLD)
        y += 1
    room = max(1, w - 14)
    for i, (name, label, _hint, _max) in enumerate(FIELDS[first:first + rows], first):
        value = values[name]
        if name == "secret":
            shown = "*" * len(value)
            if editing and not value:
                shown = "(unchanged)"
        else:
            shown = value
        if i == at:
            shown = (shown if shown != "(unchanged)" else "")[-(room - 1):] + "_"
        put(y, 0, f"{label:<10}: ", curses.A_BOLD if i == at else 0)
        put(y, 12, shown[-room:])
        y += 1
    for text, attr in footer:
        put(y, 0, text, attr)
        y += 1
    cardstar_layout.park(scr)
    scr.refresh()


async def setup_screen(scr, old, stop):
    """Ask for the settings. Returns the saved settings, or None if cancelled.
    `old` is None on first run (every field starts empty)."""
    values = {name: "" for name, *_ in FIELDS}
    if old:
        for name in values:
            if name != "secret":        # the secret is never put back on screen
                values[name] = str(old.get(name, ""))
        values["mic_gain"] = f"{clamp_gain(old.get('mic_gain')):g}"[:4]
    at, message, reveal = 0, "", False
    scr.nodelay(True)
    scr.bkgd(" ", 0)
    scr.clear()
    while not stop.is_set():
        reveal = reveal and FIELDS[at][0] == "secret"   # hide on leaving it
        _draw(scr, values, at, message, bool(old), reveal)
        try:
            key = scr.get_wch()
        except curses.error:           # nothing typed
            await asyncio.sleep(0.03)
            continue
        name, _label, _hint, longest = FIELDS[at]
        if key == "\x1b":
            return None
        if name == "secret" and key in SHOW_HIDE_KEYS:
            reveal = not reveal
            scr.clear()
            continue
        if key in ("\n", "\r", curses.KEY_ENTER):
            message = ""
            if at < len(FIELDS) - 1:
                at += 1
                continue
            cfg = dict(values)
            cfg["secret"] = cfg["secret"].strip()   # stray spaces from pasting
            if old and not cfg["secret"]:
                cfg["secret"] = old["secret"]
            bad = [(i, check(f[0], cfg[f[0]])) for i, f in enumerate(FIELDS)]
            bad = [(i, why) for i, why in bad if why]
            if bad:
                at, message = bad[0]
                continue
            cfg["port"] = int(cfg["port"] or DEFAULT_PORT)
            cfg["mic_gain"] = clamp_gain(cfg["mic_gain"] or DEFAULT_MIC_GAIN)
            cfg["compact"] = old.get("compact", "auto") if old else read_compact()
            try:
                save(cfg)
            except OSError:
                message = "Could not save the settings file"
                continue
            return cfg
        if key == curses.KEY_UP:
            at, message = max(0, at - 1), ""
        elif key in (curses.KEY_DOWN, "\t"):
            at, message = min(len(FIELDS) - 1, at + 1), ""
        elif key in ("\x7f", "\b", curses.KEY_BACKSPACE):
            values[name] = values[name][:-1]
        elif isinstance(key, str) and key.isprintable() and len(values[name]) < longest:
            values[name] += key
            message = ""
    return None


def caller_args(caller_id: str) -> dict:
    """Digits are sent as a caller number, anything else as a caller name."""
    caller_id = caller_id.strip()
    if not caller_id:
        return {}
    if re.fullmatch(r"[0-9+*#]+", caller_id):
        return {"caller_number": caller_id}
    return {"caller_name": caller_id}
