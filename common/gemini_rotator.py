from __future__ import annotations

import os
import time
from typing import Any


class AllKeysExhaustedError(Exception):
    pass


class GeminiRotator:
    """Fail over across explicitly supplied Gemini credentials.

    Quotas are commonly enforced at the project level, so multiple keys in one
    project are not treated as independent quota pools.
    """

    def __init__(self, model: str | None = None, role: str = "general"):
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        self.role = role.upper()
        configured = os.getenv(f"GEMINI_{self.role}_KEYS", "")
        if configured.strip():
            names = [name.strip() for name in configured.split(",") if name.strip()]
            self.keys = [os.getenv(name) for name in names if os.getenv(name)]
        else:
            self.keys = [
                os.getenv(f"GEMINI_API_KEY_{i}")
                for i in range(1, 6)
                if os.getenv(f"GEMINI_API_KEY_{i}")
            ]
        if not self.keys:
            raise RuntimeError(f"No Gemini credentials configured for role {role!r}.")
        self._start_index = 0
        self._cooldown_until = [0.0] * len(self.keys)

    @staticmethod
    def _is_quota_or_auth_error(exc: Exception) -> bool:
        code = getattr(exc, "code", None)
        text = str(exc).upper()
        return code in (401, 403, 429) or "RESOURCE_EXHAUSTED" in text or "RATE LIMIT" in text

    def _run(self, prompt: str, *, web: bool, max_retries_per_key: int = 1) -> str:
        try:
            from google import genai
            from google.genai import types
            from google.genai import errors as genai_errors
        except ImportError as exc:
            raise RuntimeError("google-genai is required; install requirements.txt") from exc

        last_error: Exception | None = None
        now = time.time()
        n = len(self.keys)
        for offset in range(n):
            idx = (self._start_index + offset) % n
            if self._cooldown_until[idx] > now:
                continue
            client = genai.Client(api_key=self.keys[idx])
            for _ in range(max(1, max_retries_per_key)):
                try:
                    config: Any = None
                    if web:
                        config = types.GenerateContentConfig(
                            tools=[types.Tool(google_search=types.GoogleSearch())]
                        )
                    response = client.models.generate_content(
                        model=self.model,
                        contents=prompt,
                        config=config,
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
            f"All {n} configured Gemini credentials for role {self.role} are unavailable. Last error: {last_error}"
        )

    def generate(self, prompt: str, max_retries_per_key: int = 1) -> str:
        return self._run(prompt, web=False, max_retries_per_key=max_retries_per_key)

    def generate_web(self, prompt: str, max_retries_per_key: int = 1) -> str:
        return self._run(prompt, web=True, max_retries_per_key=max_retries_per_key)
