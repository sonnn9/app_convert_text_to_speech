"""ElevenLabs API client (thin wrapper over the official REST API using
``requests``).

We use plain ``requests`` rather than the ``elevenlabs`` SDK so the code is
explicit and easy to modify (per the project requirements).

Endpoints used:
    * GET  /v1/voices                -> list available voices
    * GET  /v1/user                  -> used by "Test API" to validate the key
    * POST /v1/text-to-speech/{id}   -> synthesize speech

Errors are surfaced as :class:`ElevenLabsError` with a human-friendly message
so the GUI can show exactly what went wrong (bad key, quota, rate limit, ...).
"""

from __future__ import annotations

import time

import requests

from .models import TTSModel, Voice, VoiceSettings

API_ROOT = "https://api.elevenlabs.io"
API_BASE = f"{API_ROOT}/v1"

# Output format query param -> ElevenLabs accepts e.g. "mp3_44100_128",
# "pcm_44100" (wav-like). We request mp3 and convert to wav locally if needed
# via pydub, which keeps this layer simple and model-agnostic.
DEFAULT_TTS_OUTPUT_FORMAT = "mp3_44100_128"


class ElevenLabsError(Exception):
    """Raised for any API failure, carrying a user-friendly message."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# Models that are known NOT to accept a ``speed`` field in voice_settings.
# For these we strip ``speed`` from the request and the caller applies speed
# locally with pydub instead. Editable as ElevenLabs evolves.
MODELS_WITHOUT_SPEED: set[str] = {
    "eleven_v3",
}


class ElevenLabsClient:
    """Minimal ElevenLabs client."""

    def __init__(self, api_key: str, timeout: int = 60) -> None:
        if not api_key:
            raise ElevenLabsError("API key is empty. Please enter your ElevenLabs API key.")
        self.api_key = api_key.strip()
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"xi-api-key": self.api_key})

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _handle_error_response(self, resp: requests.Response) -> None:
        """Translate a non-2xx response into a friendly :class:`ElevenLabsError`."""
        code = resp.status_code
        # Try to extract the API's detail message.
        detail = ""
        try:
            body = resp.json()
            detail = (
                body.get("detail", {}).get("message")
                if isinstance(body.get("detail"), dict)
                else body.get("detail")
            ) or body.get("message") or ""
        except Exception:
            detail = resp.text[:300]

        if code == 401:
            raise ElevenLabsError("Invalid API key (401 Unauthorized). Please check your key.", code)
        if code == 403:
            raise ElevenLabsError(f"Forbidden (403). {detail}", code)
        if code == 422:
            raise ElevenLabsError(f"Invalid request / text too long or bad voice settings (422). {detail}", code)
        if code == 429:
            raise ElevenLabsError("Rate limit or quota exceeded (429). Slow down or check your plan.", code)
        if code == 404:
            raise ElevenLabsError(f"Not found (404) — voice_id may be invalid. {detail}", code)
        raise ElevenLabsError(f"API error {code}: {detail or resp.reason}", code)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def test_api(self) -> bool:
        """Validate the API key by hitting /v1/user. Returns True on success."""
        try:
            resp = self._session.get(f"{API_BASE}/user", timeout=self.timeout)
        except requests.RequestException as exc:
            raise ElevenLabsError(f"Network error while testing API: {exc}") from exc
        if not resp.ok:
            self._handle_error_response(resp)
        return True

    def get_voices(self) -> list[Voice]:
        """Fetch ALL voices in the account.

        Uses the paginated ``/v2/voices`` endpoint (page_size=100, following
        ``next_page_token``) so every voice is returned — not just the first
        page. Falls back to the legacy ``/v1/voices`` if v2 isn't available."""
        voices: list[Voice] = []
        token: str | None = None
        try:
            while True:
                params: dict = {"page_size": 100}
                if token:
                    params["next_page_token"] = token
                resp = self._session.get(
                    f"{API_ROOT}/v2/voices", params=params, timeout=self.timeout
                )
                if resp.status_code == 404:
                    return self._get_voices_v1()  # legacy fallback
                if not resp.ok:
                    self._handle_error_response(resp)
                data = resp.json()
                voices.extend(Voice.from_api(v) for v in data.get("voices", []))
                if not data.get("has_more"):
                    break
                token = data.get("next_page_token")
                if not token:
                    break
        except requests.RequestException as exc:
            raise ElevenLabsError(f"Network error while loading voices: {exc}") from exc
        return voices

    def _get_voices_v1(self) -> list[Voice]:
        resp = self._session.get(f"{API_BASE}/voices", timeout=self.timeout)
        if not resp.ok:
            self._handle_error_response(resp)
        return [Voice.from_api(v) for v in resp.json().get("voices", [])]

    def get_shared_voices(
        self,
        *,
        search: str = "",
        language: str = "",
        gender: str = "",
        age: str = "",
        category: str = "",
        page_size: int = 100,
        page: int = 0,
    ) -> tuple[list[Voice], bool]:
        """Browse the public Voice Library (``/v1/shared-voices``) with filters.

        Returns ``(voices, has_more)``. Empty filter values are omitted. The big
        community catalogue lives here — ``/v1/voices`` only has account voices."""
        params: dict = {"page_size": page_size, "page": page}
        if search:
            params["search"] = search
        if language:
            params["language"] = language
        if gender:
            params["gender"] = gender
        if age:
            params["age"] = age
        if category:
            params["category"] = category
        try:
            resp = self._session.get(
                f"{API_BASE}/shared-voices", params=params, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise ElevenLabsError(f"Network error while loading voice library: {exc}") from exc
        if not resp.ok:
            self._handle_error_response(resp)
        data = resp.json()
        voices = [Voice.from_shared_api(v) for v in data.get("voices", [])]
        return voices, bool(data.get("has_more", False))

    def add_shared_voice(self, public_owner_id: str, voice_id: str, new_name: str) -> str:
        """Add a Voice Library voice to the account so it can be used for TTS.

        Returns the NEW account voice_id. Raises :class:`ElevenLabsError`."""
        if not public_owner_id:
            raise ElevenLabsError("This voice has no owner id and cannot be added.")
        url = f"{API_BASE}/voices/add/{public_owner_id}/{voice_id}"
        try:
            resp = self._session.post(url, json={"new_name": new_name}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ElevenLabsError(f"Network error while adding voice: {exc}") from exc
        if not resp.ok:
            self._handle_error_response(resp)
        return resp.json().get("voice_id", voice_id)

    def get_models(self, only_tts: bool = True) -> list[TTSModel]:
        """Fetch available models. When ``only_tts`` is True (default) only
        models with ``can_do_text_to_speech == true`` are returned.

        The model capabilities (style / speaker boost / dialogue) drive which UI
        controls are enabled — so we never hard-code the model list."""
        try:
            resp = self._session.get(f"{API_BASE}/models", timeout=self.timeout)
        except requests.RequestException as exc:
            raise ElevenLabsError(f"Network error while loading models: {exc}") from exc
        if not resp.ok:
            self._handle_error_response(resp)
        data = resp.json()
        # /v1/models returns a JSON list of model objects.
        models = [TTSModel.from_api(m) for m in data] if isinstance(data, list) else []
        if only_tts:
            models = [m for m in models if m.can_do_text_to_speech]
        return models

    def text_to_speech(
        self,
        text: str,
        voice_id: str,
        model_id: str,
        voice_settings: VoiceSettings,
        output_path: str,
        output_format: str = DEFAULT_TTS_OUTPUT_FORMAT,
        pronunciation_locators: list[dict] | None = None,
    ) -> tuple[str, bool]:
        """Synthesize ``text`` and write the audio to ``output_path``.

        Returns a tuple ``(output_path, speed_applied_by_api)``:
            * ``speed_applied_by_api`` is True if the ``speed`` param was sent
              to the API. When False, the caller should apply speed locally
              with pydub (the model didn't support it).

        ``pronunciation_locators`` (optional): a list of
        ``{"pronunciation_dictionary_id": ..., "version_id": ...}`` dicts. When
        provided (and the account has uploaded dictionaries), they are sent as
        ``pronunciation_dictionary_locators``. When the app only has local
        replacement rules (no uploaded dictionary), the caller pre-substitutes
        the text instead and leaves this ``None``.

        Raises :class:`ElevenLabsError` on any failure.
        """
        if not text.strip():
            raise ElevenLabsError("Cannot synthesize empty text.")
        if not voice_id:
            raise ElevenLabsError("No voice selected for this character.")

        # Decide whether to include "speed". We optimistically send it unless
        # the model is known not to support it. If the API then rejects the
        # request with 422, we retry once without speed (graceful fallback).
        include_speed = model_id not in MODELS_WITHOUT_SPEED

        def _do_request(send_speed: bool) -> requests.Response:
            payload: dict = {
                "text": text,
                "model_id": model_id,
                "voice_settings": voice_settings.to_api_dict(include_speed=send_speed),
            }
            if pronunciation_locators:
                payload["pronunciation_dictionary_locators"] = pronunciation_locators
            url = f"{API_BASE}/text-to-speech/{voice_id}"
            params = {"output_format": output_format}
            return self._session.post(
                url, json=payload, params=params, timeout=self.timeout
            )

        try:
            resp = _do_request(include_speed)
            # Graceful fallback: model rejected the speed param.
            if resp.status_code == 422 and include_speed:
                resp = _do_request(False)
                include_speed = False
        except requests.RequestException as exc:
            raise ElevenLabsError(f"Network error during TTS: {exc}") from exc

        if not resp.ok:
            self._handle_error_response(resp)

        # The response body is the raw audio bytes.
        try:
            with open(output_path, "wb") as f:
                f.write(resp.content)
        except OSError as exc:
            raise ElevenLabsError(f"Could not write audio file: {exc}") from exc

        return output_path, include_speed


# --------------------------------------------------------------------------- #
# Rate-limit aware retry helper (exponential backoff)
# --------------------------------------------------------------------------- #
def call_with_backoff(
    func,
    *,
    max_retries: int = 4,
    base_delay: float = 2.0,
    on_retry=None,
    should_cancel=None,
):
    """Call ``func`` and retry on transient rate-limit errors (HTTP 429).

    Backoff is exponential: ``base_delay * 2**attempt`` seconds.

    Args:
        func: zero-arg callable performing the request; may raise ElevenLabsError.
        max_retries: how many extra attempts after the first.
        base_delay: initial delay in seconds.
        on_retry: optional callback ``(attempt, delay, message)`` for logging.
        should_cancel: optional zero-arg callable; if it returns True we stop
            waiting and re-raise immediately (supports Cancel during a long wait).

    Returns whatever ``func`` returns. Re-raises the last error if all attempts
    are exhausted, or for any non-429 :class:`ElevenLabsError`.
    """
    attempt = 0
    while True:
        try:
            return func()
        except ElevenLabsError as exc:
            # Only retry on rate-limit / quota throttling.
            if exc.status_code != 429 or attempt >= max_retries:
                raise
            delay = base_delay * (2 ** attempt)
            if on_retry is not None:
                on_retry(attempt + 1, delay, exc.message)
            # Sleep in small slices so a cancel can interrupt the wait.
            waited = 0.0
            while waited < delay:
                if should_cancel is not None and should_cancel():
                    raise
                time.sleep(min(0.2, delay - waited))
                waited += 0.2
            attempt += 1
