"""ElevenLabs Text-to-Dialogue API client.

The Text-to-Dialogue endpoint accepts a *cluster* of ``(text, voice_id)``
inputs and renders them as a single, naturally-flowing conversation (one audio
file) rather than synthesizing each line independently. This is useful for
multi-voice dialogue where the model benefits from cross-line context.

Endpoint used:
    * POST /v1/text-to-dialogue   -> render a list of dialogue inputs to audio

Like :mod:`app.core.elevenlabs_client`, we use plain ``requests`` (no SDK) and
surface failures as :class:`ElevenLabsError` with a human-friendly message so
the GUI can show exactly what went wrong (bad key, quota, rate limit, ...).
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests

# Reuse the shared error type and API base from the main client so error
# handling and configuration stay consistent across the app.
from app.core.elevenlabs_client import API_BASE, ElevenLabsError
from app.core.models import CharacterVoiceConfig, DialogueLine

# Default audio output format (mp3, 44.1 kHz, 128 kbps). Sent as a query param.
DEFAULT_DIALOGUE_OUTPUT_FORMAT = "mp3_44100_128"

# Full URL of the text-to-dialogue endpoint.
_DIALOGUE_URL = f"{API_BASE}/text-to-dialogue"


class DialogueAPIClient:
    """Minimal client for the ElevenLabs Text-to-Dialogue endpoint."""

    def __init__(self, api_key: str, timeout: int = 120) -> None:
        """Create the client.

        Args:
            api_key: The ElevenLabs API key. Must be non-empty.
            timeout: Per-request timeout in seconds. Dialogue rendering can be
                slower than single-line TTS, hence the larger default.

        Raises:
            ElevenLabsError: If ``api_key`` is empty.
        """
        if not api_key or not api_key.strip():
            raise ElevenLabsError("API key is empty. Please enter your ElevenLabs API key.")
        self.api_key: str = api_key.strip()
        self.timeout: int = timeout
        # A persistent session keeps the auth header on every request and reuses
        # the underlying TCP connection.
        self._session = requests.Session()
        self._session.headers.update({"xi-api-key": self.api_key})

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _handle_error_response(self, resp: requests.Response) -> None:
        """Translate a non-2xx response into a friendly :class:`ElevenLabsError`.

        Mirrors the status handling in :class:`ElevenLabsClient` so the GUI sees
        consistent messages regardless of which endpoint failed.
        """
        code = resp.status_code
        # Try to extract the API's detail message; fall back to raw text.
        detail = ""
        try:
            body = resp.json()
            detail = (
                body.get("detail", {}).get("message")
                if isinstance(body.get("detail"), dict)
                else body.get("detail")
            ) or body.get("message") or ""
        except Exception:
            # Response was not JSON (e.g. audio bytes or an HTML error page).
            detail = resp.text[:300]

        if code == 401:
            raise ElevenLabsError("Invalid API key (401 Unauthorized). Please check your key.", code)
        if code == 403:
            raise ElevenLabsError(f"Forbidden (403). {detail}", code)
        if code == 422:
            raise ElevenLabsError(
                f"Invalid request / text too long or unsupported settings (422). {detail}", code
            )
        if code == 429:
            raise ElevenLabsError("Rate limit or quota exceeded (429). Slow down or check your plan.", code)
        if code == 404:
            raise ElevenLabsError(
                f"Not found (404) — text-to-dialogue may be unavailable or a voice_id is invalid. {detail}",
                code,
            )
        raise ElevenLabsError(f"API error {code}: {detail or resp.reason}", code)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def text_to_dialogue(
        self,
        inputs: list[dict],
        model_id: str,
        output_path: str,
        output_format: str = DEFAULT_DIALOGUE_OUTPUT_FORMAT,
        settings: Optional[dict] = None,
    ) -> str:
        """Render a cluster of dialogue ``inputs`` to a single audio file.

        Args:
            inputs: List of ``{"text": str, "voice_id": str}`` dicts (use
                :func:`build_inputs` to construct these from dialogue lines).
            model_id: The ElevenLabs model id to use for rendering.
            output_path: Where to write the resulting audio (parent directories
                are created automatically).
            output_format: ElevenLabs ``output_format`` value (default mp3).
            settings: Optional ``settings`` object to include in the request
                body (omitted when ``None``).

        Returns:
            The ``output_path`` that was written.

        Raises:
            ElevenLabsError: On empty inputs, network failure, a non-2xx
                response, or a file write error.
        """
        if not inputs:
            raise ElevenLabsError("Cannot render dialogue: no inputs were provided.")

        # Build the JSON request body. ``settings`` is optional per the API.
        payload: dict[str, Any] = {
            "inputs": inputs,
            "model_id": model_id,
        }
        if settings is not None:
            payload["settings"] = settings

        params = {"output_format": output_format}

        try:
            resp = self._session.post(
                _DIALOGUE_URL,
                json=payload,
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ElevenLabsError(f"Network error during dialogue render: {exc}") from exc

        if not resp.ok:
            self._handle_error_response(resp)

        # On success the response body is the raw audio bytes. Ensure the
        # destination directory exists before writing.
        try:
            parent = os.path.dirname(os.path.abspath(output_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(resp.content)
        except OSError as exc:
            raise ElevenLabsError(f"Could not write dialogue audio file: {exc}") from exc

        return output_path

    def is_supported(self) -> bool:
        """Best-effort check that text-to-dialogue is available for this account.

        We probe the endpoint cheaply and interpret the result:
            * 404 / 405  -> endpoint does not exist for this account => False
            * 401        -> endpoint exists but the key is unauthorized => True
                            (the endpoint itself is present)
            * any other  -> endpoint exists / reachable => True
            * network or other exception -> False (never raises)

        Returns:
            True if the endpoint appears to exist, False otherwise.
        """
        try:
            # Try a lightweight OPTIONS request first; many servers answer it
            # without doing real work.
            resp = self._session.options(_DIALOGUE_URL, timeout=self.timeout)
            status = resp.status_code

            # Some deployments don't implement OPTIONS (501) or reject the
            # method (405) even when the endpoint exists for POST. In that case
            # fall back to a tiny POST probe to disambiguate.
            if status in (405, 501):
                resp = self._session.post(
                    _DIALOGUE_URL,
                    json={"inputs": [], "model_id": ""},
                    params={"output_format": DEFAULT_DIALOGUE_OUTPUT_FORMAT},
                    timeout=self.timeout,
                )
                status = resp.status_code

            # 404 means the route itself is not present for this account.
            if status == 404:
                return False
            # 405 here (after the POST fallback) would still mean the path
            # exists but rejects the method — treat the route as present.
            # Everything else (401 unauthorized, 422 bad body, 2xx, 429, ...)
            # implies the endpoint exists.
            return True
        except requests.RequestException:
            # Network error / DNS / timeout: we cannot confirm support.
            return False
        except Exception:
            # Defensive: never let this probe raise.
            return False


def build_inputs(
    lines: list[DialogueLine],
    configs: dict[str, CharacterVoiceConfig],
) -> list[dict]:
    """Build the ``inputs`` list for :meth:`DialogueAPIClient.text_to_dialogue`.

    Each dialogue line is mapped to ``{"text": ..., "voice_id": ...}`` using the
    character's voice configuration. Lines whose character has no config, or a
    config without a ``voice_id``, are skipped (they cannot be rendered).

    Args:
        lines: Ordered dialogue lines to render.
        configs: Mapping of character name -> :class:`CharacterVoiceConfig`.

    Returns:
        A list of ``{"text": str, "voice_id": str}`` dicts in line order.
    """
    inputs: list[dict] = []
    for line in lines:
        config = configs.get(line.character)
        # Skip lines without a usable voice mapping.
        if config is None or not config.voice_id:
            continue
        inputs.append(
            {
                "text": line.api_text(),
                "voice_id": config.voice_id,
            }
        )
    return inputs
