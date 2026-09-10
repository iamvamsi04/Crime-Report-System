"""Minimal Gemini client that accepts only explicitly supplied prompt content."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.config import Settings
from app.core.exceptions import LLMConfigurationError


logger = logging.getLogger(__name__)


class GeminiClient:
    """Gemini transport; it has no access to local documents or calculation tools."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.settings.gemini_api_key:
                raise LLMConfigurationError(
                    "GEMINI_API_KEY is missing. Add it to .env before asking natural-language questions."
                )
            try:
                from google import genai
            except ImportError as exc:
                raise LLMConfigurationError(
                    "google-genai is not installed. Run 'pip install -r requirements.txt'."
                ) from exc
            self._client = genai.Client(api_key=self.settings.gemini_api_key)
        return self._client

    def generate_json(self, prompt: str) -> dict[str, Any]:
        """Request JSON with deterministic settings and reject malformed model output."""
        try:
            from google.genai import types

            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )
            text = (response.text or "").strip()
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("Gemini did not return a JSON object.")
            return payload
        except LLMConfigurationError:
            raise
        except Exception as exc:
            logger.exception("Gemini request failed")
            raise LLMConfigurationError("Gemini could not complete the request. Please try again.") from exc
