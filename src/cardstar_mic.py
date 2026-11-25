# CardStar - microphone gain on the ES8389 sound chip.
# Copyright (C) 2026 TheBeardedHamm
# SPDX-License-Identifier: GPL-2.0-only
"""Raise the mic gain while CardStar runs, then put the old value back.
Does nothing on hardware without the ES8389 chip."""
import glob
import json
import os
import re
import subprocess

import cardstar_setup

CARD_IDS = "/proc/asound/card*/id"
CONTROLS = ("ADCL PGA", "ADCR PGA")
# Mic gain on the audio chip (0-14, 8 = 24 dB default, 11 = 33 dB).
MIC_PGA = 11


def _find_card():
    for path in sorted(glob.glob(CARD_IDS)):
        try:
            with open(path) as f:
                card = f.read().strip()
        except OSError:
            continue
        if "ES8389" in card.upper():
            return card
    return None


def _amixer(*args):
    try:
        done = subprocess.run(["amixer", *args], timeout=5, check=False,
                              capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


class MicGain:
    def __init__(self):
        self.card = None
        self.original = {}

    @property
    def _state_file(self):
        # Remembers the original value in case the app is force-killed.
        return os.path.join(cardstar_setup.CONFIG_DIR, "mic-gain-original")

    def _remembered(self) -> dict:
        try:
            with open(self._state_file) as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if k in CONTROLS and isinstance(v, int)}
        except (OSError, ValueError, AttributeError):
            return {}

    def apply(self) -> None:
        self.card = _find_card()
        if not self.card:
            return
        remembered = self._remembered()
        for ch in CONTROLS:
            out = _amixer("-c", self.card, "sget", ch)
            found = re.search(r":\s*(?:Playback |Capture )?(\d+) \[", out or "")
            if not found:
                continue
            now = int(found.group(1))
            # Still at our value with a note left over: the last run was killed.
            self.original[ch] = remembered.get(ch, now) if now == MIC_PGA else now
        if not self.original:
            return
        try:
            cardstar_setup.make_private()
            fd = os.open(self._state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.original, f)
        except OSError:
            pass
        for ch in self.original:
            _amixer("-q", "-c", self.card, "sset", ch, str(MIC_PGA))

    def restore(self) -> None:
        if not self.original:
            return
        ok = True
        for ch, value in self.original.items():
            ok = _amixer("-q", "-c", self.card, "sset", ch, str(value)) is not None and ok
        if ok:
            self.original = {}
            try:
                os.unlink(self._state_file)
            except OSError:
                pass
