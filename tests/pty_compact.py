# CardStar - run the real app in a 24x80 pty and check where it draws.
# Copyright (C) 2026 TheBeardedHamm
# SPDX-License-Identifier: GPL-2.0-only
"""Run from the project folder:  python3 tests/pty_compact.py

Starts the app in a pseudo-terminal that reports 24 rows x 80 columns (like
gh0st-term), with compact mode forced on. A small local IAX2 server (made-up
login), a fake pacat (silent mic, discarded speaker) and a throwaway HOME are
used, so nothing real is touched. Every character the app prints, and every
coloured erase, is tracked: all must stay inside the top-left 10x46 area, and
the cursor must rest inside it. The made-up secret must never be printed.
A control run in full mode must draw outside the area (proves the check works).
"""
import asyncio
import codecs
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import tempfile
import termios
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(TOP, "vendor")]
import iax2  # noqa: E402

ROWS, COLS = 24, 80          # what the pty reports
AREA_R, AREA_C = 10, 46      # where compact mode may draw
USER, SECRET, NODE = "testuser", "made-up-secret-0451", "12345"


# ---- a small VT100/xterm screen model ---------------------------------
class Screen:
    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.r = self.c = 0
        self.wrap_next = False
        self.bg = False            # a background colour / reverse is active
        self.top, self.bottom = 0, rows - 1
        self.saved = (0, 0)
        self.last = " "
        self.outside = []          # (what, row, col) drawn outside the area
        self.unknown = set()
        self.dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.state, self.buf = "ground", ""

    def in_area(self, r, c):
        return r < AREA_R and c < AREA_C

    def mark(self, what, r, c):
        if not self.in_area(r, c):
            self.outside.append((what, r, c))

    def put(self, ch):
        if self.wrap_next:
            self.mark("autowrap", self.r, self.c)
            self.c, self.wrap_next = 0, False
            self.lf()
        self.grid[self.r][self.c] = ch
        if ch != " " or self.bg:
            self.mark(f"char {ch!r}", self.r, self.c)
        self.last = ch
        if self.c == self.cols - 1:
            self.wrap_next = True
        else:
            self.c += 1

    def lf(self):
        if self.r == self.bottom:
            self.mark("scroll", self.r, self.c)
            del self.grid[self.top]
            self.grid.insert(self.bottom, [" "] * self.cols)
        else:
            self.r = min(self.rows - 1, self.r + 1)

    def erase(self, r, c0, c1):
        for c in range(c0, c1):
            self.grid[r][c] = " "
            if self.bg:
                self.mark("coloured erase", r, c)

    def feed(self, data: bytes):
        for ch in self.dec.decode(data):
            self.step(ch)

    def step(self, ch):
        if self.state == "ground":
            if ch == "\x1b":
                self.state, self.buf = "esc", ""
            elif ch == "\r":
                self.c, self.wrap_next = 0, False
            elif ch in "\n\x0b\x0c":
                self.wrap_next = False
                self.lf()
            elif ch == "\b":
                self.c, self.wrap_next = max(0, self.c - 1), False
            elif ch == "\t":
                self.c = min(self.cols - 1, (self.c // 8 + 1) * 8)
            elif ch in "\x07\x0e\x0f\x00":
                pass
            elif ch >= " ":
                self.put(ch)
            else:
                self.unknown.add(repr(ch))
        elif self.state == "esc":
            if ch == "[":
                self.state = "csi"
            elif ch == "]":
                self.state = "osc"
            elif ch in "()*+#":
                self.state = "charset"
            else:
                self.state = "ground"
                if ch == "7":
                    self.saved = (self.r, self.c)
                elif ch == "8":
                    self.r, self.c = self.saved
                elif ch == "M":                    # reverse index
                    self.r = max(0, self.r - 1)
                elif ch == "E":
                    self.c = 0
                    self.lf()
                elif ch == "D":
                    self.lf()
                elif ch not in "=>c":
                    self.unknown.add("ESC " + ch)
        elif self.state == "charset":
            self.state = "ground"
        elif self.state == "osc":
            if ch == "\x07" or (self.buf.endswith("\x1b") and ch == "\\"):
                self.state = "ground"
            self.buf += ch
        elif self.state == "csi":
            if "\x40" <= ch <= "\x7e":
                self.csi(self.buf, ch)
                self.state = "ground"
            else:
                self.buf += ch

    def csi(self, params, final):
        private = params[:1] in ("?", ">", "<", "=")
        nums = [int(p) if p.isdigit() else None for p in params.lstrip("?><=").split(";")]
        n = nums[0] if nums and nums[0] else 1
        self.wrap_next = False
        if private:
            return                         # modes (cursor show/hide, keypad, ...)
        if final in "Hf":
            r = (nums[0] or 1) if nums else 1
            c = (nums[1] or 1) if len(nums) > 1 else 1
            self.r, self.c = min(self.rows, r) - 1, min(self.cols, c) - 1
        elif final == "A":
            self.r = max(0, self.r - n)
        elif final in "Be":
            self.r = min(self.rows - 1, self.r + n)
        elif final in "Ca":
            self.c = min(self.cols - 1, self.c + n)
        elif final == "D":
            self.c = max(0, self.c - n)
        elif final in "G`":
            self.c = min(self.cols, n) - 1
        elif final == "d":
            self.r = min(self.rows, n) - 1
        elif final == "J":
            mode = nums[0] or 0
            spans = {0: [(self.r, self.c, self.cols)] + [(r, 0, self.cols) for r in range(self.r + 1, self.rows)],
                     1: [(r, 0, self.cols) for r in range(self.r)] + [(self.r, 0, self.c + 1)],
                     2: [(r, 0, self.cols) for r in range(self.rows)],
                     3: []}.get(mode, [])
            for r, a, b in spans:
                self.erase(r, a, b)
        elif final == "K":
            mode = nums[0] or 0
            a, b = {0: (self.c, self.cols), 1: (0, self.c + 1), 2: (0, self.cols)}[mode]
            self.erase(self.r, a, b)
        elif final == "X":
            self.erase(self.r, self.c, min(self.cols, self.c + n))
        elif final == "b":                 # repeat the last character
            for _ in range(n):
                self.put(self.last)
        elif final == "@":
            row = self.grid[self.r]
            row[self.c:self.c] = [" "] * n
            del row[self.cols:]
            if any(ch != " " for ch in row[AREA_C:]):
                self.mark("insert pushed text", self.r, AREA_C)
        elif final == "P":
            row = self.grid[self.r]
            del row[self.c:self.c + n]
            row.extend([" "] * (self.cols - len(row)))
        elif final in "LM":
            pass                           # whole-line insert/delete (blank lines)
        elif final == "m":
            self.sgr([p or 0 for p in nums] if params else [0])
        elif final == "r":
            self.top = (nums[0] or 1) - 1 if nums else 0
            self.bottom = (nums[1] or ROWS) - 1 if len(nums) > 1 else ROWS - 1
            self.r = self.c = 0
        elif final == "s":
            self.saved = (self.r, self.c)
        elif final == "u":
            self.r, self.c = self.saved
        elif final in "hlnct":
            pass
        else:
            self.unknown.add(f"CSI {params}{final}")

    def sgr(self, ps):
        i = 0
        while i < len(ps):
            p = ps[i]
            if p == 0 or p == 27 or p == 49:
                self.bg = False if p != 27 else self.bg and False
            elif p == 7 or 40 <= p <= 47 or 100 <= p <= 107:
                self.bg = True
            elif p in (38, 48):
                if p == 48:
                    self.bg = True
                i += 2 if i + 1 < len(ps) and ps[i + 1] == 5 else 4
            i += 1

    def text(self, rows=None, cols=None):
        return "\n".join("".join(row[:cols]).rstrip() for row in self.grid[:rows])


# ---- fake server -------------------------------------------------------
class FakeServer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.port = None
        self.ready = threading.Event()
        self.keyed = False

    def run(self):
        asyncio.run(self.main())

    async def main(self):
        creds = iax2.Credentials(username=USER, secret=SECRET)
        async with iax2.Endpoint(host="127.0.0.1", port=0, users={USER: creds}) as ep:
            self.port = ep.local_address[1]
            self.ready.set()
            async for call in ep.incoming():
                asyncio.create_task(self.serve(call))

    async def serve(self, call):
        await call.answer()
        silence = bytes(320)
        while not call.is_ended:
            call.send_pcm(silence)
            await asyncio.sleep(0.02)


# ---- running the app ---------------------------------------------------
class App:
    def __init__(self, home, args, env_extra=()):
        self.screen = Screen(ROWS, COLS)
        self.raw = b""
        self.cursor_checks = []
        env = {"HOME": home, "PATH": os.path.join(home, "bin") + ":/usr/bin:/bin",
               "TERM": os.environ.get("CARDSTAR_TEST_TERM", "xterm-256color"),
               "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        env.update(env_extra)
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable,
                      [sys.executable, "-I", "-B", os.path.join(home, "app", "cardstar.py"), *args],
                      env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        os.kill(pid, signal.SIGWINCH)
        self.pid, self.fd = pid, fd

    def pump(self, seconds, until=None):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    return False
                if not data:
                    return False
                self.raw += data
                self.screen.feed(data)
            elif until is not None and re.search(until, self.screen.text()):
                return True
        return until is None

    def settle(self):
        """Wait for a quiet moment and note where the cursor rests."""
        self.pump(0.4)
        self.cursor_checks.append((self.screen.r, self.screen.c))

    def key(self, text, wait=0.4):
        os.write(self.fd, text.encode() if isinstance(text, str) else text)
        self.pump(wait)

    def quit(self):
        try:
            os.write(self.fd, b"q")
        except OSError:
            pass
        self.pump(3)
        try:
            _, status = os.waitpid(self.pid, 0)
        except ChildProcessError:
            status = 0
        os.close(self.fd)
        return status


def make_home(port, write_config=True):
    home = tempfile.mkdtemp(prefix="cardstar-pty-")
    os.makedirs(os.path.join(home, "bin"))
    # The app laid out as in the package: the .py files with vendor/ beside them.
    app = os.path.join(home, "app")
    shutil.copytree(os.path.join(TOP, "src"), app,
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(os.path.join(TOP, "vendor"), os.path.join(app, "vendor"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    pacat = os.path.join(home, "bin", "pacat")
    with open(pacat, "w") as f:
        f.write("#!/bin/sh\n"
                "case \" $* \" in *' --record '*)\n"
                f"  exec {sys.executable} -I -c 'import sys,time\n"
                "while True:\n"
                "    sys.stdout.buffer.write(bytes(320)); sys.stdout.flush(); time.sleep(0.02)' ;;\n"
                "esac\nexec cat >/dev/null\n")
    os.chmod(pacat, 0o755)
    if write_config:
        cfg = os.path.join(home, ".config", "cardstar")
        os.makedirs(cfg, mode=0o700)
        with open(os.path.join(cfg, "config"), "w") as f:
            json.dump({"host": "127.0.0.1", "port": port, "username": USER, "secret": SECRET,
                       "node": NODE, "caller_id": "", "mic_gain": 4.0, "compact": "auto"}, f)
        os.chmod(os.path.join(cfg, "config"), 0o600)
    return home


RESULTS = []


def check(name, ok, note=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  - ' + note if note else ''}")


def area_report(app, name):
    s = app.screen
    check(f"{name}: nothing drawn outside 10x46", not s.outside,
          f"{len(s.outside)} cells, first {s.outside[:3]}" if s.outside else "")
    bad = [p for p in app.cursor_checks if not s.in_area(*p)]
    check(f"{name}: cursor rests inside the area ({len(app.cursor_checks)} checks)", not bad,
          f"outside at {bad[:3]}" if bad else "")
    check(f"{name}: secret never printed", SECRET.encode() not in app.raw)
    if s.unknown:
        print(f"      note: sequences not modelled: {sorted(s.unknown)[:8]}")


def main():
    if shutil.which("infocmp") is None:
        print("note: infocmp missing; terminal names are not checked")
    server = FakeServer()
    server.start()
    server.ready.wait(5)

    # 1. Compact forced, full session: main, connect, DTMF, TX, setup, quit.
    home = make_home(server.port)
    app = App(home, ["--compact", "--connect"])
    shots = {}
    check("compact: connects to the fake server", app.pump(15, r"Status: CONNECTED"))
    app.settle()
    shots["main (connected)"] = app.screen.text(AREA_R, AREA_C)
    app.key("+")
    app.key("-")
    app.key("-")
    check("compact: volume shown", "Vol 3/5 auto" in app.screen.text())
    app.key("*3")
    app.settle()
    shots["typing DTMF"] = app.screen.text(AREA_R, AREA_C)
    app.key("\r")
    app.settle()
    check("compact: DTMF sent shown", "Sent: *3" in app.screen.text())
    app.key(" ", 1.5)
    ok = app.pump(3, r"TRANSMITTING")
    app.settle()
    shots["transmitting"] = app.screen.text(AREA_R, AREA_C)
    check("compact: TX screen", ok and "auto-stop in" in app.screen.text())
    app.key(" ", 1.5)
    app.settle()
    check("compact: TX stopped", "TRANSMITTING" not in app.screen.text())
    app.key("d", 1.5)
    app.settle()
    shots["disconnected"] = app.screen.text(AREA_R, AREA_C)
    app.key("s", 1.0)
    app.settle()
    shots["setup (editing)"] = app.screen.text(AREA_R, AREA_C)
    check("compact: setup screen", "Mic gain" in app.screen.text())
    for _ in range(3):
        app.key("\x1bOB", 0.3)               # down to the secret field
    app.key("\x1bOD", 0.6)                  # Left = show
    app.settle()
    shots["secret shown (saved secret)"] = app.screen.text(AREA_R, AREA_C)
    check("compact: show-secret view", "Secret shown" in app.screen.text())
    app.key("\x1bOC", 0.6)                  # Right = hide
    app.settle()
    for _ in range(6):
        app.key("\x1bOB", 0.2)
    app.key("\x1b", 1.0)                    # Esc = back
    app.settle()
    shots["back from setup"] = app.screen.text(AREA_R, AREA_C)
    status = app.quit()
    check("compact: quits cleanly with q", os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0)
    area_report(app, "compact session")
    with open(os.path.join(home, ".config", "cardstar", "config")) as f:
        check("compact: settings file still has compact key", json.load(f).get("compact") == "auto")
    shutil.rmtree(home)

    # 2. Compact via CARDSTAR_COMPACT, first run (no settings file).
    home = make_home(server.port, write_config=False)
    app = App(home, [], {"CARDSTAR_COMPACT": "1"})
    check("first run: setup screen shown", app.pump(8, r"Server"))
    app.settle()
    shots["first-run setup"] = app.screen.text(AREA_R, AREA_C)
    app.key("\x1b", 1.5)                    # Esc = quit on first run
    status = app.quit()
    area_report(app, "first run (CARDSTAR_COMPACT=1)")
    shutil.rmtree(home)

    # 3. Control: --no-compact at 24x80 uses the whole screen.
    home = make_home(server.port)
    app = App(home, ["--no-compact"])
    app.pump(5, r"c=connect")
    app.settle()
    full = app.screen.text()
    app.quit()
    check("control: full layout draws outside the area (checker works)",
          bool(app.screen.outside) and "c=connect" in full.splitlines()[ROWS - 1])
    shutil.rmtree(home)

    print("\nCompact screens as drawn (top-left 10x46 of the 24x80 pty):")
    for name, text in shots.items():
        lines = (text.splitlines() + [""] * AREA_R)[:AREA_R]
        print(f"\n[{name}]\n+" + "-" * AREA_C + "+")
        for line in lines:
            print("|" + line.ljust(AREA_C) + "|")
        print("+" + "-" * AREA_C + "+")
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} pty checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
