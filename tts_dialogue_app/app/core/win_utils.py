"""Windows-specific helpers.

The most important one: :func:`suppress_subprocess_windows`.

pydub shells out to ``ffmpeg`` / ``ffprobe`` via :class:`subprocess.Popen`
*without* passing any "hide window" flags. On Windows — especially in a
``--windowed`` PyInstaller build that has no console of its own — every one of
those calls flashes a black console window. During a conversion that's dozens
of pop-ups.

We fix it globally by monkey-patching ``subprocess.Popen.__init__`` to default
``creationflags`` to ``CREATE_NO_WINDOW`` (and hide the window via
``STARTUPINFO``) whenever the caller didn't ask for anything else. This affects
every subprocess the app spawns (all of them are ffmpeg/ffprobe), so audio
runs completely in the background with no flicker.
"""

from __future__ import annotations

import subprocess
import sys

# https://learn.microsoft.com/windows/win32/procthread/process-creation-flags
CREATE_NO_WINDOW = 0x08000000

_patched = False


def suppress_subprocess_windows() -> None:
    """Patch subprocess so child processes (ffmpeg/ffprobe) never show a
    console window. No-op on non-Windows platforms and idempotent."""
    global _patched
    if _patched or not sys.platform.startswith("win"):
        return
    _patched = True

    _orig_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Only inject when the caller hasn't specified creation flags itself,
        # so we never override an intentional choice elsewhere.
        if "creationflags" not in kwargs:
            kwargs["creationflags"] = CREATE_NO_WINDOW

        # Belt-and-suspenders: also hide via STARTUPINFO.
        startupinfo = kwargs.get("startupinfo")
        if startupinfo is None:
            startupinfo = subprocess.STARTUPINFO()
        try:
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE  # type: ignore[attr-defined]
            kwargs["startupinfo"] = startupinfo
        except AttributeError:
            # STARTF_USESHOWWINDOW/SW_HIDE only exist on Windows; ignore otherwise.
            pass

        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init  # type: ignore[assignment]
