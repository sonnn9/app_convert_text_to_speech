"""Local audio cache for the TTS Dialogue App.

Identical dialogue lines (same text + voice + model + settings) produce identical
audio from the ElevenLabs API. Re-calling the API for them wastes quota and time,
so we cache rendered audio on disk keyed by a SHA256 hash of the request inputs.

The cache is a flat directory of ``<sha256>.<ext>`` files. Looking up a key is a
single ``os.path.exists`` check; storing copies the freshly rendered file in.

Only stdlib is used (``hashlib``, ``os``, ``shutil``) — no audio library is needed
here because the cache treats files as opaque blobs.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from typing import Optional

# Import the existing model — do NOT redefine it. Only used for typing here.
from app.core.models import VoiceSettings


class CacheManager:
    """Disk-backed audio cache keyed by SHA256 of the TTS request inputs.

    Parameters
    ----------
    cache_dir:
        Directory where cached audio files live. Created on init if missing.
    enabled:
        When ``False``, :meth:`get` always returns ``None`` (cache misses), so
        the app re-renders everything. Storing (:meth:`put`) still works, which
        keeps the cache warm even while temporarily disabled for reads.
    """

    def __init__(self, cache_dir: str, enabled: bool = True) -> None:
        self.cache_dir: str = cache_dir
        self.enabled: bool = enabled
        # Always ensure the directory exists; size/clear/count rely on it.
        os.makedirs(self.cache_dir, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Key derivation
    # ------------------------------------------------------------------ #
    @staticmethod
    def make_key(
        text: str,
        voice_id: str,
        model_id: str,
        settings: "VoiceSettings",
    ) -> str:
        """Return the SHA256 hex digest identifying a TTS request.

        The hash is computed over a canonical string built from every input
        that affects the rendered audio: the text, voice id, model id, and each
        voice setting. Floats are rounded to 4 decimals so that insignificant
        precision noise (e.g. ``0.30000000000000004``) does not fragment the
        cache. Fields are joined with a non-textual separator (NUL byte) to
        avoid accidental collisions between adjacent fields.
        """
        # Round floats for stable, reproducible keys regardless of tiny
        # floating-point representation differences.
        stability = round(float(settings.stability), 4)
        similarity_boost = round(float(settings.similarity_boost), 4)
        style = round(float(settings.style), 4)
        use_speaker_boost = bool(settings.use_speaker_boost)
        speed = round(float(settings.speed), 4)

        # NUL separator keeps fields unambiguous (it cannot appear in normal
        # text/ids), preventing e.g. "ab"+"c" colliding with "a"+"bc".
        canonical = "\x00".join(
            [
                text,
                voice_id,
                model_id,
                f"{stability:.4f}",
                f"{similarity_boost:.4f}",
                f"{style:.4f}",
                str(use_speaker_boost),
                f"{speed:.4f}",
            ]
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    # Path helpers
    # ------------------------------------------------------------------ #
    def path_for(self, key: str, ext: str) -> str:
        """Return the absolute-ish path ``<cache_dir>/<key>.<ext>`` for a key.

        ``ext`` may be passed with or without a leading dot; it is normalized.
        """
        ext = ext.lstrip(".")
        return os.path.join(self.cache_dir, f"{key}.{ext}")

    # ------------------------------------------------------------------ #
    # Read / write
    # ------------------------------------------------------------------ #
    def get(self, key: str, ext: str) -> Optional[str]:
        """Return the cached file path if available, else ``None``.

        Returns ``None`` when caching is disabled or the file does not exist.
        """
        if not self.enabled:
            return None
        path = self.path_for(key, ext)
        return path if os.path.isfile(path) else None

    def put(self, key: str, src_path: str, ext: str) -> str:
        """Store ``src_path`` in the cache as ``<key>.<ext>`` and return its path.

        Uses :func:`shutil.copy2` to preserve metadata. The cache directory is
        (re)created if needed. If ``src_path`` already resolves to the target
        cache path, no copy is performed and the path is returned as-is.
        """
        dest = self.path_for(key, ext)
        # Avoid copying a file onto itself (would raise SameFileError or worse).
        if os.path.abspath(src_path) == os.path.abspath(dest):
            return dest
        os.makedirs(self.cache_dir, exist_ok=True)
        shutil.copy2(src_path, dest)
        return dest

    def copy_to(self, key: str, ext: str, dest_path: str) -> Optional[str]:
        """Copy a cached entry out to ``dest_path``.

        Creates the destination directory if necessary. Returns ``dest_path``
        on success, or ``None`` if there is no cache entry for ``key``/``ext``.

        Note: this performs the copy regardless of the ``enabled`` flag — the
        flag only governs :meth:`get`. The lookup here checks file existence
        directly so a warm cache can still be exported when reads are disabled.
        """
        src = self.path_for(key, ext)
        if not os.path.isfile(src):
            return None
        parent = os.path.dirname(dest_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(src, dest_path)
        return dest_path

    # ------------------------------------------------------------------ #
    # Stats / maintenance
    # ------------------------------------------------------------------ #
    def _iter_files(self) -> list[str]:
        """Return absolute paths of all regular files directly in the cache dir."""
        if not os.path.isdir(self.cache_dir):
            return []
        files: list[str] = []
        for name in os.listdir(self.cache_dir):
            full = os.path.join(self.cache_dir, name)
            if os.path.isfile(full):
                files.append(full)
        return files

    def size_bytes(self) -> int:
        """Total size in bytes of all files in the cache directory."""
        total = 0
        for full in self._iter_files():
            try:
                total += os.path.getsize(full)
            except OSError:
                # File vanished between listing and stat; ignore it.
                continue
        return total

    def size_human(self) -> str:
        """Human-readable cache size, e.g. ``"12.3 MB"`` or ``"512 B"``."""
        size = float(self.size_bytes())
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        idx = 0
        while size >= 1024.0 and idx < len(units) - 1:
            size /= 1024.0
            idx += 1
        # Bytes are whole numbers; larger units get one decimal place.
        if idx == 0:
            return f"{int(size)} {units[idx]}"
        return f"{size:.1f} {units[idx]}"

    def clear(self) -> int:
        """Delete every file in the cache directory; return the number removed."""
        removed = 0
        for full in self._iter_files():
            try:
                os.remove(full)
                removed += 1
            except OSError:
                # Skip files we cannot delete (locked, permissions, etc.).
                continue
        return removed

    def count(self) -> int:
        """Return the number of cached files."""
        return len(self._iter_files())
