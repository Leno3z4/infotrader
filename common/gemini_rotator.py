from __future__ import annotations

import os
import time


class AllKeysExhaustedError(Exception):
    pass


class GeminiRotator:
    """Fail over across explicitly supplied Gemini API keys.

    Gemini rate limits are enforced per Google Cloud project, not per API key.
    Multiple keys in one project do not multiply that project's quota.
    """

    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.keys = [
            os.getenv(f"GEMINI_API_KEY_{i}")
            for i in range(1, 6)
            if os.getenv(f"GEMINI_API_KEY_{i}")
        ]
        if not self.keys:
            raise RuntimeError("Set at least GEMINI_API_KEY_1.")
        self._start_index = 0
        self._cooldown_until = [0.0] * len(self.keys)

    @staticmethod
    def _is_quota_or_auth_error(exc: Exception) -> bool:
        code = getattr(exc, "code", None)
        text = str(exc).upper()
        return code in (401, 403, 429) or "RESOURCE_EXHAUSTED" in text

    def generate(self, prompt: str, max_retries_per_key: int = 1) -> str:
        try:
            from google import genai
            from google.genai import errors as genai_errors
        except ImportError as exc:
            raise RuntimeError(
                "google-genai is required to call Gemini; install requirements.txt"
            ) from exc

        last_error = None
        now = time.time()
        n = len(self.keys)

        for offset in range(n):
            idx = (self._start_index + offset) % n
            if self._cooldown_until[idx] > now:
                continue

            client = genai.Client(api_key=self.keys[idx])
            for _ in range(max(1, max_retries_per_key)):
                try:
                    response = client.models.generate_content(
                        model=self.model,
                        contents=prompt,
                    )
                    text = getattr(response, "text", None)
                    if not text:
                        raise RuntimeError("Gemini returned an empty response.")
                    self._start_index = idx
                    return text
                except genai_errors.APIError as exc:
                    last_error = exc
                    if self._is_quota_or_auth_error(exc):
                        self._cooldown_until[idx] = time.time() + 300
                        break
                    time.sleep(1)
                except Exception as exc:
                    last_error = exc
                    break

        raise AllKeysExhaustedError(
            f"All {n} configured Gemini keys/projects are unavailable. Last error: {last_error}"
        )
