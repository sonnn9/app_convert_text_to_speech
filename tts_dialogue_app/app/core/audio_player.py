"""Robust local audio playback.

QtMultimedia's backend is fragile inside a PyInstaller ``--windowed`` build on
Windows (often *silent* because the media backend DLLs aren't bundled). Since
this app already requires **ffmpeg**, we play preview/line audio with **ffplay**
(ships with ffmpeg) as a subprocess — hidden, no window — which is far more
reliable. If ffplay can't be found we fall back to a provided ``QMediaPlayer``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional


def find_ffplay() -> Optional[str]:
    """Locate ffplay: PATH, next to ffmpeg (pydub.converter), or next to the app."""
    p = shutil.which("ffplay")
    if p:
        return p
    # next to the configured ffmpeg binary
    try:
        from pydub import AudioSegment

        conv = getattr(AudioSegment, "converter", None)
        if conv and os.path.isfile(conv):
            cand = os.path.join(os.path.dirname(conv), "ffplay.exe")
            if os.path.isfile(cand):
                return cand
    except Exception:
        pass
    # next to the app/exe
    base = (
        os.path.dirname(sys.executable)
        if getattr(sys, "frozen", False)
        else os.getcwd()
    )
    cand = os.path.join(base, "ffplay.exe")
    return cand if os.path.isfile(cand) else None


class AudioPlayer:
    """Play local audio files. Prefers ffplay; falls back to a QMediaPlayer.

    ``log`` is an optional callable used once to report which backend is active.
    """

    def __init__(self, qplayer=None, log=None) -> None:
        self._qplayer = qplayer
        self._log = log or (lambda *_: None)
        self._ffplay = find_ffplay()
        self._proc: Optional[subprocess.Popen] = None
        self._announced = False

    @property
    def backend(self) -> str:
        return "ffplay" if self._ffplay else ("QMediaPlayer" if self._qplayer else "none")

    def play(self, path: str) -> None:
        self.stop()
        abspath = os.path.abspath(path)
        if not self._announced:
            self._log(f"Audio backend: {self.backend}")
            self._announced = True

        if self._ffplay:
            try:
                # -nodisp: no window, -autoexit: quit at EOF, quiet logs.
                # Console window is suppressed globally (win_utils patch).
                self._proc = subprocess.Popen(
                    [self._ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", abspath]
                )
                return
            except Exception as exc:
                self._log(f"ffplay failed ({exc}); falling back to QMediaPlayer.")

        # Fallback: Qt
        if self._qplayer is not None:
            from PySide6.QtCore import QUrl

            self._qplayer.stop()
            self._qplayer.setSource(QUrl.fromLocalFile(abspath))
            self._qplayer.play()

    def play_url(self, url: str) -> None:
        """Stream a remote URL (used as a last-resort fallback)."""
        self.stop()
        if self._ffplay:
            try:
                self._proc = subprocess.Popen(
                    [self._ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", url]
                )
                return
            except Exception:
                pass
        if self._qplayer is not None:
            from PySide6.QtCore import QUrl

            self._qplayer.stop()
            self._qplayer.setSource(QUrl(url))
            self._qplayer.play()

    def stop(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        if self._qplayer is not None:
            try:
                self._qplayer.stop()
            except Exception:
                pass
