#!/usr/bin/env python3
# CardStar - an unofficial AllStar (IAX2) client for the CardputerZero.
# Copyright (C) 2026 TheBeardedHamm
# SPDX-License-Identifier: GPL-2.0-only
"""Not affiliated with or endorsed by AllStarLink.

Keys:  c = connect   d = disconnect   s = setup   q = quit
       Space = PTT: press once to transmit, press again to stop
       + / -  = receive volume up / down (1-5, automatic level)
       0-9 * #  = type a DTMF command (while connected)
       Enter = send it   Backspace = fix a digit   Esc = clear
"""
import argparse
import array
import asyncio
import curses
import logging
import math
import os
import signal
import sys
import time

# Use the library shipped inside this package.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "vendor")]

import iax2  # noqa: E402

import cardstar_mic  # noqa: E402
import cardstar_setup  # noqa: E402

# Receive audio: played through PipeWire with pacat (8 kHz, 16-bit, mono).
PLAYER = ["pacat", "--playback", "--raw", "--format=s16le", "--rate=8000",
          "--channels=1", "--latency-msec=80", "--client-name=cardstar"]
JITTER_MIN_MS = 200      # audio cushion against uneven Wi-Fi (tested: 60 ms
JITTER_MAX_MS = 500      # dropped ~12% of packets, 200 ms dropped ~0.1%)
RX_THRESHOLD = 400       # sample level that counts as "someone talking"
# Receive volume is automatic: each station is brought to the same loudness.
RX_VOL_START = 4         # +/- picks 1..5; each step is a target peak level
RX_VOL_TARGETS = {1: 4000, 2: 8000, 3: 14000, 4: 20000, 5: 28000}
RX_GAIN_MAX = 30.0       # never boost more than this
RX_NOISE = 150           # below this it's silence/hiss: don't chase it


class AutoGain:
    """Simple automatic volume: turn down fast on loud audio,
    turn up slowly on quiet audio, hold still during silence."""

    def __init__(self):
        self.gain = 3.0
        self.env = 0.0       # recent peak level of the incoming audio

    def process(self, pcm: bytes, target: int) -> bytes:
        peak = max(map(abs, array.array("h", pcm)))
        self.env = max(peak, self.env * 0.995)
        if self.env > RX_NOISE:
            want = min(RX_GAIN_MAX, max(1.0, target / self.env))
            if want < self.gain:
                self.gain = want                          # fast down
            else:
                self.gain += (want - self.gain) * 0.02    # slow up
        return boost(pcm, self.gain)


def boost(pcm: bytes, gain: float) -> bytes:
    """Make 16-bit audio louder, capping peaks so they don't wrap around."""
    if gain == 1.0:
        return pcm
    s = array.array("h", pcm)
    for i, v in enumerate(s):
        v = int(v * gain)
        s[i] = 32767 if v > 32767 else -32768 if v < -32768 else v
    return s.tobytes()

def mic_boost(pcm: bytes, gain: float) -> bytes:
    """Apply the mic gain. Up to the normal 4x this is a plain multiply. Above
    it, loud peaks are rounded off smoothly so they do not clip harshly."""
    if gain <= MIC_GAIN:
        return boost(pcm, gain)
    s = array.array("h", pcm)
    room = 32767 - LIMIT_KNEE
    for i, v in enumerate(s):
        x = v * gain
        if abs(x) > LIMIT_KNEE:
            x = math.copysign(LIMIT_KNEE + room * math.tanh((abs(x) - LIMIT_KNEE) / room), x)
        s[i] = int(x)
    return s.tobytes()


# Transmit audio: recorded from the mic with pacat, 20 ms at a time.
RECORDER = ["pacat", "--record", "--raw", "--format=s16le", "--rate=8000",
            "--channels=1", "--latency-msec=40", "--client-name=cardstar-mic"]
FRAME_BYTES = 320        # 20 ms of 8 kHz 16-bit mono
MIC_GAIN = cardstar_setup.DEFAULT_MIC_GAIN  # the built-in mic is quiet; boost it
LIMIT_KNEE = 24000       # above the normal gain, peaks past this are squeezed
TX_TIMEOUT = 120         # seconds: auto-stop transmitting (safety)

# Auto-reconnect when the link drops (Wi-Fi, server restart). Waits grow
# 5 -> 10 -> 20 -> 40 -> 60 s so we don't hammer the server.
RECONNECT_FIRST = 5
RECONNECT_MAX = 60


# ---- connection logic -------------------------------------------------
class Client:
    def __init__(self, ep: iax2.Endpoint, cfg: dict):
        self.ep = ep
        self.cfg = cfg
        self.call = None
        self.status = "IDLE"
        self.detail = "Press c to connect"
        self.since = None          # time the call was answered
        self._task = None          # running connect attempt
        self.want_connected = False  # True between "connect" and "disconnect"
        self._retry = None         # running reconnect loop
        self.tx_since = None       # time PTT was pressed (None = not transmitting)
        self.tx_peak = 0           # mic level while transmitting (for display)
        self.tx_best = 0           # loudest level since PTT was pressed
        self.rx_until = 0.0        # show RX until this time
        self.rx_vol = RX_VOL_START
        self.agc = AutoGain()
        self.rx_frames = self.rx_lost = 0
        self.typed = ""            # DTMF command being typed (not sent yet)
        self.dtmf = ""             # last command sent (for display)
        self._dtmf_q = asyncio.Queue()
        asyncio.create_task(self._dtmf_worker())

    def type_digit(self, digit: str) -> None:
        if self.connected:
            self.typed = (self.typed + digit)[:20]
        else:
            self.detail = "Not connected - press c first"

    def send_typed(self) -> None:
        """Send the whole typed command in one quick burst, because
        AllStar forgets a command if digits are ~3 s apart."""
        if self.connected and self.typed:
            self._dtmf_q.put_nowait(self.typed)
            self.dtmf, self.typed = self.typed, ""

    async def _dtmf_worker(self) -> None:
        while True:
            digits = await self._dtmf_q.get()
            if self.connected:
                try:
                    await self.call.send_dtmf(digits)
                except Exception as e:
                    self.set("CONNECTED", f"DTMF error: {type(e).__name__}")

    def set(self, status: str, detail: str = "") -> None:
        self.status, self.detail = status, detail

    @property
    def connected(self) -> bool:
        return self.call is not None and not self.call.is_ended

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def start_connect(self) -> None:
        self.want_connected = True
        if self.connected or self.busy:    # already on, or an attempt is running
            return
        if self._retry and not self._retry.done():
            self._retry.cancel()           # "c" during the countdown = try now
        self._task = asyncio.create_task(self._connect())

    def link_lost(self, why: str) -> None:
        """The call ended without us asking: start reconnecting."""
        self.ptt_off()
        self.set("DISCONNECTED", why)
        if self.want_connected and not (self._retry and not self._retry.done()):
            self._retry = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay, attempt = RECONNECT_FIRST, 0
        while self.want_connected and not self.connected:
            attempt += 1
            for left in range(delay, 0, -1):
                self.status = "RECONNECTING"
                self.detail = f"Link lost - retry {attempt} in {left}s"
                await asyncio.sleep(1)
                if not self.want_connected:
                    return
            self._task = asyncio.create_task(self._connect())
            await self._task
            delay = min(delay * 2, RECONNECT_MAX)

    async def _connect(self) -> None:
        cfg = self.cfg
        self.set("CONNECTING", f"{cfg['username']} -> {cfg['node']}")
        try:
            call = await self.ep.dial(
                cfg["host"], cfg["node"], port=cfg["port"],
                username=cfg["username"], secret=cfg["secret"],
                **cardstar_setup.caller_args(cfg["caller_id"]),
                formats=[iax2.Format.ULAW],
                timeout=20,
            )
        except iax2.AuthenticationError:
            self.want_connected = False     # retrying a wrong secret is pointless
            self.set("FAILED", "Login refused - s=setup")
            return
        except iax2.CallRejected as e:
            self.want_connected = False
            if "no authority" in str(e).lower():     # bad username or secret
                self.set("FAILED", "Login refused - s=setup")
            else:
                self.set("FAILED", f"Rejected: {e}")
            return
        except (iax2.CallTimeout, TimeoutError):
            self.set("FAILED", "No answer from server")
            return
        except Exception as e:  # keep running whatever happens (e.g. no Wi-Fi)
            self.set("FAILED", f"{type(e).__name__}: {e}")
            if self.want_connected and not (self._retry and not self._retry.done()):
                self._retry = asyncio.create_task(self._reconnect_loop())
            return
        self.call = call
        self.since = time.monotonic()
        self.set("CONNECTED", f"Node {cfg['node']}")
        asyncio.create_task(self._watch(call))
        asyncio.create_task(self._receive(call))
        asyncio.create_task(self._transmit(call))

    @property
    def rx(self) -> bool:
        return self.connected and not self.tx and time.monotonic() < self.rx_until

    # -- transmit -------------------------------------------------------
    @property
    def tx(self) -> bool:
        return self.tx_since is not None

    def toggle_ptt(self) -> None:
        if self.tx:
            self.ptt_off()
        elif self.connected:
            self.call.radio_key()
            self.tx_since = time.monotonic()
            self.tx_peak = self.tx_best = 0
        else:
            self.detail = "Not connected - press c first"

    def ptt_off(self, why: str = "") -> None:
        if not self.tx:
            return
        self.tx_since = None
        if self.connected:
            try:
                self.call.radio_unkey()
            except Exception:
                pass
        if why:
            self.detail = why

    async def _transmit(self, call) -> None:
        """Read the mic all the time; send it only while PTT is on."""
        try:
            mic = await asyncio.create_subprocess_exec(
                *RECORDER, stdout=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        except OSError as e:
            self.detail = f"No mic: {e}"
            return
        try:
            while not call.is_ended:
                pcm = await mic.stdout.readexactly(FRAME_BYTES)
                if not self.tx:
                    continue
                if time.monotonic() - self.tx_since > TX_TIMEOUT:
                    self.ptt_off(f"TX stopped: {TX_TIMEOUT}s limit")
                    continue
                pcm = mic_boost(pcm, self.cfg["mic_gain"])
                peak = max(map(abs, array.array("h", pcm)))
                self.tx_peak = max(self.tx_peak * 0.9, peak)
                self.tx_best = max(self.tx_best, peak)
                call.send_pcm(pcm)
        except asyncio.IncompleteReadError:
            self.detail = "Mic stopped"
        except Exception as e:
            self.detail = f"TX error: {type(e).__name__}"
        finally:
            self.ptt_off()
            if mic.returncode is None:
                mic.terminate()
            await mic.wait()

    async def _receive(self, call) -> None:
        """Play received audio on the speaker and light up RX."""
        self.rx_frames = self.rx_lost = 0
        try:
            player = await asyncio.create_subprocess_exec(
                *PLAYER, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        except OSError as e:
            self.detail = f"No speaker: {e}"
            return
        try:
            async for frame in call.playout(min_delay=JITTER_MIN_MS, max_delay=JITTER_MAX_MS):
                try:
                    pcm = frame.pcm
                except iax2.CodecError:
                    continue
                if frame.lost and self.rx_frames == 0:
                    continue          # padding before first packet: not a loss
                self.rx_frames += 1
                if frame.lost:
                    self.rx_lost += 1
                elif max(map(abs, array.array("h", pcm))) > RX_THRESHOLD:
                    self.rx_until = time.monotonic() + 0.4
                if self.tx:
                    pcm = bytes(len(pcm))   # mute speaker while transmitting
                else:
                    pcm = self.agc.process(pcm, RX_VOL_TARGETS[self.rx_vol])
                player.stdin.write(pcm)
                await player.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self.detail = "Speaker player stopped"
        finally:
            try:
                player.stdin.close()
            except Exception:
                pass
            if player.returncode is None:
                player.terminate()
            await player.wait()

    async def _watch(self, call) -> None:
        """Notice when the server ends the call."""
        async for ev in call.events():
            if isinstance(ev, iax2.HangupEvent) and ev.by_peer:
                why = ev.cause or ev.reason or f"cause {ev.cause_code}"
                self.link_lost(f"By server: {why}")

    async def disconnect(self) -> None:
        self.want_connected = False          # user asked: no reconnecting
        if self._retry and not self._retry.done():
            self._retry.cancel()
            self.set("IDLE", "Reconnect cancelled")
        self.ptt_off()
        if self.busy:
            self._task.cancel()
            self.set("IDLE", "Connect cancelled")
        if self.connected:
            self.set("DISCONNECTING")
            try:
                await self.call.hangup()
            finally:
                self.set("DISCONNECTED", "Hung up cleanly")
        self.call = None
        self.since = None
        self.dtmf = ""
        self.typed = ""


# ---- screen (fits 38 x 8) ---------------------------------------------
COLORS = {"CONNECTED": 1, "CONNECTING": 2, "DISCONNECTING": 2, "RECONNECTING": 2,
          "FAILED": 3}
HAS_COLOR = False


def setup_colors() -> None:
    """Use colours if the terminal has them. The launcher's terminal
    reports itself as a plain 'vt100', so fall back to black & white."""
    global HAS_COLOR
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_GREEN, bg)
        curses.init_pair(2, curses.COLOR_YELLOW, bg)
        curses.init_pair(3, curses.COLOR_RED, bg)
        curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLUE)   # RX block
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_GREEN)  # TX = live mic
        HAS_COLOR = True
    except curses.error:
        HAS_COLOR = False


def pair(n: int) -> int:
    return curses.color_pair(n) if HAS_COLOR else 0


_bg = [None]  # current window background (so we only change it when needed)


def draw(scr, c: Client) -> None:
    # While transmitting, the whole window background is green, so every
    # cell (edges included) is filled - no black gaps.
    big = (pair(5) if HAS_COLOR else curses.A_REVERSE) | curses.A_BOLD
    want_bg = big if c.tx else 0
    if _bg[0] != want_bg:
        scr.bkgd(" ", want_bg)
        scr.clear()               # repaint everything once on the switch
        _bg[0] = want_bg
    scr.erase()
    h, w = scr.getmaxyx()

    def put(y, x, text, attr=0):
        if y < h:
            try:
                scr.addnstr(y, x, text, max(0, w - x - 1), attr)
            except curses.error:
                pass

    if c.tx:  # whole screen becomes a big TX banner
        left =max(0, TX_TIMEOUT - int(time.monotonic() - c.tx_since))
        bar = "#" * min(w - 12, int(c.tx_peak / 32767 * (w - 12) + 0.5))
        lines = ["", ">>>  TRANSMITTING  <<<", "",
                 "ON AIR - node " + c.cfg["node"], f"auto-stop in {left:3d}s",
                 f"gain {c.cfg['mic_gain']:g}x   loudest {min(100, c.tx_best * 100 // 32767)}%",
                 f"mic [{bar:<{w - 12}}]", "SPACE = stop TX"]
        for y in range(h):
            text = lines[y] if y < len(lines) else ""
            put(y, 0, text.center(w - 1), big)
        scr.refresh()
        return

    put(0, 0, f"CardStar {c.cfg['node']}  {c.cfg['username']}", curses.A_BOLD)
    put(1, 0, "-" * (w - 1))
    color = pair(COLORS.get(c.status, 0)) | curses.A_BOLD
    put(2, 0, "Status: ")
    put(2, 8, c.status, color)
    if c.rx:  # big green RX block, top right
        rx = (pair(4) if HAS_COLOR else curses.A_REVERSE) | curses.A_BOLD
        put(2, w - 7, "      ", rx)
        put(3, w - 7, "  RX  ", rx)
        put(4, w - 7, "      ", rx)
    if c.since is not None and c.connected:
        s = int(time.monotonic() - c.since)
        put(3, 0, f"Time:   {s // 60:02d}:{s % 60:02d}")
    put(4, 0, c.detail)
    if c.connected:
        put(5, 0, "Type: ")
        if c.typed:
            put(5, 6, c.typed + "_  Enter=send", curses.A_BOLD)
        else:
            put(5, 6, "0-9 * # then Enter", curses.A_DIM)
        if c.dtmf:
            put(6, 0, f"Sent: {c.dtmf}")
    put(3, 15, f"Vol {c.rx_vol}/5 auto")
    if c.connected:
        put(h - 1, 0, "SPACE=PTT d=disconnect q=quit", curses.A_DIM)
    else:
        put(h - 1, 0, "c=connect d=disconnect s=setup q=quit", curses.A_DIM)
    scr.refresh()


async def ui(scr, c: Client, stop: asyncio.Event, autoconnect: bool) -> None:
    try:
        curses.curs_set(0)          # hide cursor (not possible on every terminal)
    except curses.error:
        pass
    scr.nodelay(True)
    if autoconnect:
        c.start_connect()
    last_space = 0.0
    while not stop.is_set():
        draw(scr, c)
        key = scr.getch()
        if key == ord(" "):
            # Holding Space repeats the key; only the first press counts.
            now = time.monotonic()
            if now - last_space > 0.3:
                c.toggle_ptt()
            last_space = now
        elif key in (ord("+"), ord("=")):
            c.rx_vol = min(5, c.rx_vol + 1)
        elif key in (ord("-"), ord("_")):
            c.rx_vol = max(1, c.rx_vol - 1)
        elif key in (ord("c"), ord("C")):
            c.start_connect()
        elif key in (ord("d"), ord("D")):
            await c.disconnect()
        elif key in (ord("q"), ord("Q")):
            return
        elif key in (ord("s"), ord("S")):
            if c.connected:
                c.detail = "Press d first, then s"
            else:
                await c.disconnect()           # stop any retry in progress
                new = await cardstar_setup.setup_screen(scr, c.cfg, stop)
                scr.clear()
                _bg[0] = None
                if new:
                    c.cfg = new
                    c.set("IDLE", "Settings saved")
                    if autoconnect:
                        c.start_connect()
        elif 0 <= key < 256 and chr(key) in "0123456789*#":
            c.type_digit(chr(key))
        elif key in (10, 13, curses.KEY_ENTER):
            c.send_typed()
        elif key in (8, 127, curses.KEY_BACKSPACE):
            c.typed = c.typed[:-1]
        elif key == 27:  # Esc
            c.typed = ""
        await asyncio.sleep(0.05)


async def run(scr, autoconnect: bool) -> None:
    # If the launcher closes the terminal, leave cleanly: hang up, restore mic.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    cfg = cardstar_setup.load()
    if cfg is None:                            # first run
        cfg = await cardstar_setup.setup_screen(scr, None, stop)
        scr.clear()
        if cfg is None:
            return
    mic = cardstar_mic.MicGain()
    try:
        mic.apply()
        async with iax2.Endpoint(port=0) as ep:   # any free local port
            c = Client(ep, cfg)
            try:
                await ui(scr, c, stop, autoconnect)
            finally:
                await c.disconnect()           # always hang up on exit
    finally:
        mic.restore()


def main() -> None:
    p = argparse.ArgumentParser(prog="cardstar")
    p.add_argument("--connect", action="store_true",
                   help="connect straight away when the app opens")
    args = p.parse_args()

    # CardStar keeps no log file. This also stops library messages, which could
    # name the server or user, from reaching the screen or any file.
    logging.disable(logging.CRITICAL)

    def start(scr):
        setup_colors()
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(100)
        asyncio.run(run(scr, args.connect))

    try:
        curses.wrapper(start)
    except KeyboardInterrupt:
        pass  # run()'s finally already hung up
    except Exception as e:
        # Only the kind of error: the message could contain a setting.
        print(f"CardStar stopped: {type(e).__name__}")
        sys.exit(1)


if __name__ == "__main__":
    main()
