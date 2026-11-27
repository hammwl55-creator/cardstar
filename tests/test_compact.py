# CardStar - checks for the compact (46x10) layout.
# Copyright (C) 2026 TheBeardedHamm
# SPDX-License-Identifier: GPL-2.0-only
"""Run from the project folder:  python3 -m unittest discover -s tests -v
Every screen is drawn into a fake 10x46 window. All values are made up, and
nothing here prints any setting."""
import asyncio
import json
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path[:0] = [SRC, os.path.join(os.path.dirname(HERE), "vendor")]

import cardstar  # noqa: E402
import cardstar_layout as layout  # noqa: E402
import cardstar_setup as setup  # noqa: E402

ROWS, COLS = layout.ROWS, layout.COLS
SECRET = "made-up-secret-" + "x" * 113          # 128 characters, the longest
FULL_NOTE = setup.FORCE_QUIT_NOTE


class FakeWin:
    """Just enough of a curses window. Remembers what was drawn and where;
    a write outside the window is recorded as a problem (real curses would
    raise curses.error, which the app quietly ignores)."""

    def __init__(self, h=ROWS, w=COLS, keys=()):
        self.h, self.w = h, w
        self.keys = list(keys)
        self.problems = []
        self.cut = []                  # texts that did not fit and were cut
        self.cursor = (0, 0)
        self.parked = None
        self.erase()

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.grid = [[" "] * self.w for _ in range(self.h)]

    clear = erase

    def bkgd(self, *_):
        pass

    def nodelay(self, *_):
        pass

    def move(self, y, x):
        if not (0 <= y < self.h and 0 <= x < self.w):
            self.problems.append(("move", y, x))
            return
        self.cursor = (y, x)

    def addnstr(self, y, x, text, n, attr=0):
        drawn = text[:n]
        if len(drawn) < len(text):
            self.cut.append(text)
        if not (0 <= y < self.h and x >= 0 and x + len(drawn) <= self.w):
            self.problems.append(("write", y, x, len(drawn)))
            return
        for i, ch in enumerate(drawn):
            self.grid[y][x + i] = ch
        self.cursor = (y, min(self.w - 1, x + len(drawn)))

    def refresh(self):
        self.parked = self.cursor

    def get_wch(self):
        if not self.keys:
            raise cardstar.curses.error("no input")
        return self.keys.pop(0)

    def lines(self):
        return ["".join(row).rstrip() for row in self.grid]

    def text(self):
        return "\n".join(self.lines())


def client(**kw):
    c = types.SimpleNamespace(
        tx=False, tx_since=None, tx_peak=0, tx_best=0, rx=False, since=None,
        connected=False, status="IDLE", detail="Press c to connect", typed="",
        dtmf="", rx_vol=4,
        cfg={"node": "12345", "username": "testuser", "mic_gain": 4.0})
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def now():
    return cardstar.time.monotonic()


MAIN_SCREENS = {
    "idle": {},
    "connecting": dict(status="CONNECTING", detail="testuser -> 12345"),
    "failed": dict(status="FAILED", detail="No answer from server"),
    "failed-plaintext": dict(status="FAILED", detail="Server wants plain-text login: refused"),
    "failed-long": dict(status="FAILED", detail="OSError: " + "a very long reason " * 6),
    "reconnecting": dict(status="RECONNECTING", detail="Link lost - retry 12 in 60s"),
    "connected": dict(status="CONNECTED", detail="Node 12345", connected=True,
                      since=now() - 3725),
    "connected-rx": dict(status="CONNECTED", detail="Node 12345", connected=True,
                         rx=True, since=now() - 5),
    "connected-typing": dict(status="CONNECTED", detail="Node 12345", connected=True,
                             typed="*" + "1" * 19, dtmf="*3" + "9" * 18, since=now()),
    "disconnecting": dict(status="DISCONNECTING", detail=""),
    "long-names": dict(cfg={"node": "9" * 20, "username": "u" * 64, "mic_gain": 4.0}),
}
for vol in range(1, 6):
    MAIN_SCREENS[f"volume-{vol}"] = dict(rx_vol=vol, status="CONNECTED",
                                         connected=True, since=now())
TX_SCREENS = {
    "tx-start": dict(tx=True, tx_since=now(), connected=True),
    "tx-loud": dict(tx=True, tx_since=now() - 119, tx_peak=32767, tx_best=32767,
                    connected=True,
                    cfg={"node": "9" * 20, "username": "u" * 64, "mic_gain": 10.0}),
    "tx-gain-1": dict(tx=True, tx_since=now() - 60, tx_peak=9000, tx_best=12000,
                      connected=True,
                      cfg={"node": "12345", "username": "testuser", "mic_gain": 1.0}),
}

SETUP_VALUES = {
    "host": "h" * 253, "port": "65535", "username": "u" * 64, "secret": SECRET,
    "node": "9" * 20, "caller_id": "c" * 64, "mic_gain": "10",
}


class CompactBase(unittest.TestCase):
    def setUp(self):
        self._was = layout.COMPACT
        layout.COMPACT = True
        cardstar._bg[0] = None

    def tearDown(self):
        layout.COMPACT = self._was

    def check_fits(self, win, name):
        self.assertEqual(win.problems, [], f"{name}: drawn outside 10x46")
        lines = win.lines()
        self.assertLessEqual(len(lines), ROWS, name)
        for line in lines:
            self.assertLessEqual(len(line), COLS, f"{name}: line too long")
        self.assertEqual(win.parked, (0, 0), f"{name}: cursor not parked")


class TestMainScreens(CompactBase):
    def test_every_main_screen_fits(self):
        for name, kw in {**MAIN_SCREENS, **TX_SCREENS}.items():
            with self.subTest(screen=name):
                win = FakeWin()
                cardstar.draw(win, client(**kw))
                self.check_fits(win, name)

    def test_footer_and_short_warning_shown_whole(self):
        for name in ("idle", "failed", "connected"):
            win = FakeWin()
            cardstar.draw(win, client(**MAIN_SCREENS[name]))
            text = win.text()
            self.assertIn(layout.FORCE_QUIT_SHORT, text, name)
            self.assertNotIn(FULL_NOTE, text, name)
            keys = ("SPACE=PTT d=disconnect q=quit" if name == "connected"
                    else "c=connect d=disconnect s=setup q=quit")
            self.assertEqual(win.lines()[ROWS - 1], keys, name)
            self.assertIn("CardStar 12345", win.lines()[0], name)

    def test_status_time_volume_visible(self):
        win = FakeWin()
        cardstar.draw(win, client(**MAIN_SCREENS["connected-rx"]))
        text = win.text()
        for want in ("Status: CONNECTED", "Time:   00:05", "Vol 4/5 auto", "RX",
                     "0-9 * # then Enter"):
            self.assertIn(want, text)

    def test_tx_screen_contents(self):
        win = FakeWin()
        cardstar.draw(win, client(**TX_SCREENS["tx-loud"]))
        text = win.text()
        for want in ("TRANSMITTING", "auto-stop in", "gain 10x", "loudest 100%",
                     "SPACE = stop TX", layout.FORCE_QUIT_SHORT):
            self.assertIn(want, text)

    def test_short_warning_fits(self):
        self.assertLessEqual(len(layout.FORCE_QUIT_SHORT), COLS - 1)


class TestSetupScreens(CompactBase):
    def test_every_field_fits(self):
        for editing in (False, True):
            for at in range(len(setup.FIELDS)):
                for message in ("", "Node number is needed (digits only)",
                                "Could not save the settings file"):
                    for values in (SETUP_VALUES, {k: "" for k in SETUP_VALUES}):
                        name = f"setup at={at} editing={editing} msg={bool(message)}"
                        with self.subTest(screen=name, full=bool(values["host"])):
                            win = FakeWin()
                            setup._draw(win, dict(values), at, message, editing, False)
                            self.check_fits(win, name)

    def test_all_fields_and_footer_visible(self):
        win = FakeWin()
        setup._draw(win, dict(SETUP_VALUES), 0, "", True, False)
        text = win.text()
        for _name, label, _hint, _max in setup.FIELDS:
            self.assertIn(f"{label:<10}: ", text)
        self.assertIn(layout.FORCE_QUIT_SHORT, text)
        self.assertEqual(win.lines()[ROWS - 1], setup.footer_keys(True))

    def test_show_hide_secret_fits(self):
        at = [f[0] for f in setup.FIELDS].index("secret")
        for editing, secret in ((False, SECRET), (True, SECRET), (True, ""), (False, "")):
            values = dict(SETUP_VALUES, secret=secret)
            for reveal in (False, True):
                with self.subTest(editing=editing, reveal=reveal, empty=not secret):
                    win = FakeWin()
                    setup._draw(win, values, at, "", editing, reveal)
                    self.check_fits(win, "secret view")
                    shown = win.text().replace("\n", "")
                    if reveal and secret:   # all of it must be readable
                        self.assertIn(SECRET + "_", shown)
                    else:
                        self.assertNotIn(SECRET[:20], shown)


class TestFullLayoutUnchanged(unittest.TestCase):
    """With compact off, the full layout is what it was."""

    def test_full_note_used(self):
        layout.COMPACT = False
        win = FakeWin(24, 80)
        cardstar._bg[0] = None
        cardstar.draw(win, client())
        self.assertIn(FULL_NOTE, win.text())
        self.assertEqual(win.lines()[23], "c=connect d=disconnect s=setup q=quit")
        self.assertEqual(win.parked[0], 23)       # cursor not moved

    def test_area_is_stdscr_when_full(self):
        layout.COMPACT = False
        sentinel = object()
        self.assertIs(layout.area(sentinel), sentinel)


class TestChoose(unittest.TestCase):
    big, small = (24, 80), (12, 50)

    def test_order(self):
        c = layout.choose
        self.assertEqual(c(False, "1", True, self.small, gh0st=True), (False, "command line"))
        self.assertEqual(c(True, "0", False, self.big, gh0st=False), (True, "command line"))
        self.assertEqual(c(None, "0", True, self.small, gh0st=True), (False, "CARDSTAR_COMPACT"))
        self.assertEqual(c(None, "1", False, self.big, gh0st=False), (True, "CARDSTAR_COMPACT"))
        self.assertEqual(c(None, None, False, self.small, gh0st=True), (False, "settings file"))
        self.assertEqual(c(None, "", True, self.big, gh0st=False), (True, "settings file"))
        self.assertEqual(c(None, None, "auto", self.big, gh0st=True), (True, "auto: gh0st-term"))
        self.assertEqual(c(None, None, "auto", (23, 80), gh0st=False),
                         (True, "auto: small terminal"))
        self.assertEqual(c(None, None, "auto", (24, 79), gh0st=False),
                         (True, "auto: small terminal"))
        self.assertEqual(c(None, None, "auto", self.big, gh0st=False), (False, "auto"))
        self.assertEqual(c(None, "banana", "banana", (50, 200), gh0st=False), (False, "auto"))

    def test_values(self):
        for v in ("1", "true", "YES", "on", True):
            self.assertIs(layout.parse_switch(v), True)
        for v in ("0", "false", "No", "off", False):
            self.assertIs(layout.parse_switch(v), False)
        for v in ("auto", "", None, 1, "2"):
            self.assertIsNone(layout.parse_switch(v))
            self.assertEqual(layout.config_value(v), "auto")


class TestProcWalk(unittest.TestCase):
    def make(self, tree):
        """tree: {pid: (comm, ppid, argv)} written as a fake /proc."""
        root = tempfile.mkdtemp()
        for pid, (comm, ppid, argv) in tree.items():
            os.makedirs(os.path.join(root, str(pid)))
            with open(os.path.join(root, str(pid), "stat"), "w") as f:
                f.write(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194560 0 0\n")
            with open(os.path.join(root, str(pid), "cmdline"), "wb") as f:
                f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        return root

    def test_found_as_program(self):
        proc = self.make({1: ("systemd", 0, ["/sbin/init"]),
                          150: ("gh0st-term", 1, ["/usr/bin/gh0st-term"]),
                          200: ("sh", 150, ["/bin/sh", "-c", "cardstar"]),
                          300: ("python3", 200, ["/usr/bin/python3", "cardstar.py"])})
        self.assertTrue(layout.under_gh0st_term(300, proc))
        self.assertTrue(layout.under_gh0st_term(200, proc))

    def test_found_as_script(self):
        proc = self.make({1: ("systemd", 0, ["/sbin/init"]),
                          150: ("python3", 1, ["python3", "/opt/gh0st/gh0st-term.py"]),
                          300: ("weird ) name", 150, ["x"])})
        self.assertTrue(layout.under_gh0st_term(300, proc))

    def test_not_found(self):
        proc = self.make({1: ("systemd", 0, ["/sbin/init"]),
                          150: ("sshd", 1, ["sshd: testuser"]),
                          200: ("bash", 150, ["-bash"]),
                          300: ("python3", 200, ["python3", "cardstar.py"])})
        self.assertFalse(layout.under_gh0st_term(300, proc))
        self.assertFalse(layout.under_gh0st_term(999, proc))       # no such pid
        self.assertFalse(layout.under_gh0st_term(300, "/no/such/proc"))

    def test_loop_ends(self):
        proc = self.make({5: ("a", 6, ["a"]), 6: ("b", 5, ["b"])})
        self.assertFalse(layout.under_gh0st_term(5, proc))


class TestSettingsFile(unittest.TestCase):
    OLD = {"host": "example.org", "port": 4569, "username": "testuser",
           "secret": "made-up-secret", "node": "12345", "caller_id": "", "mic_gain": 4.0}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved = setup.CONFIG_DIR, setup.CONFIG_FILE
        setup.CONFIG_DIR = os.path.join(self.dir, "cardstar")
        setup.CONFIG_FILE = os.path.join(setup.CONFIG_DIR, "config")

    def tearDown(self):
        setup.CONFIG_DIR, setup.CONFIG_FILE = self._saved

    def write(self, data):
        setup.make_private()
        with open(setup.CONFIG_FILE, "w") as f:
            json.dump(data, f)

    def read(self):
        with open(setup.CONFIG_FILE) as f:
            return json.load(f)

    def test_old_file_loads_as_auto(self):
        self.write(self.OLD)
        cfg = setup.load()
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["compact"], "auto")
        self.assertEqual(setup.read_compact(), "auto")

    def test_values_and_bad_value(self):
        for stored, want in ((True, True), (False, False), ("auto", "auto"),
                             ("banana", "auto"), (None, "auto"), (7, "auto")):
            self.write(dict(self.OLD, compact=stored))
            cfg = setup.load()
            self.assertIsNotNone(cfg, stored)       # an odd value never breaks loading
            self.assertEqual(cfg["compact"], want)
            self.assertEqual(setup.read_compact(), want)

    def test_read_compact_without_full_settings(self):
        self.assertEqual(setup.read_compact(), "auto")           # no file
        self.write({"compact": False})                           # incomplete file
        self.assertIsNone(setup.load())
        self.assertIs(setup.read_compact(), False)

    def test_save_keeps_key(self):
        self.write(dict(self.OLD, compact=False))
        cfg = {k: v for k, v in self.OLD.items()}               # no "compact"
        setup.save(cfg)
        self.assertIs(self.read()["compact"], False)

    def run_setup(self, old, keys):
        win = FakeWin(keys=keys)
        stop = asyncio.Event()
        return asyncio.run(setup.setup_screen(win, old, stop)), win

    def test_setup_screen_keeps_key(self):
        self.write(dict(self.OLD, compact=False))
        old = setup.load()
        new, win = self.run_setup(old, ["\n"] * len(setup.FIELDS))
        self.assertIsNotNone(new)
        data = self.read()
        self.assertIs(data["compact"], False)
        self.assertEqual(data["secret"], self.OLD["secret"])     # blank kept it
        self.assertIs(setup.load()["compact"], False)

    def test_first_run_keeps_key_from_incomplete_file(self):
        self.write({"compact": True})
        keys = (list("example.org") + ["\n", "\n"] + list("testuser") + ["\n"]
                + list("made-up-secret") + ["\n"] + list("12345") + ["\n", "\n", "\n"])
        new, _ = self.run_setup(None, keys)
        self.assertIsNotNone(new)
        self.assertIs(self.read()["compact"], True)

    def test_first_run_writes_auto(self):
        keys = (list("example.org") + ["\n", "\n"] + list("testuser") + ["\n"]
                + list("made-up-secret") + ["\n"] + list("12345") + ["\n", "\n", "\n"])
        new, _ = self.run_setup(None, keys)
        self.assertIsNotNone(new)
        self.assertEqual(self.read()["compact"], "auto")
        self.assertEqual(oct(os.stat(setup.CONFIG_FILE).st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
