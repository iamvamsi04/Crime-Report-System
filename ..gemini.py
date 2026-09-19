from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.errors import EmbeddingError, GeminiError

log = logging.getLogger(__name__)


class GeminiService(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    def generate_json(self, system: str, user: str) -> dict[str, Any]: ...

    def generate_text(self, system: str, user: str) -> str: ...


class GeminiClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if not self.settings.gemini_api_key:
            raise GeminiError("Gemini API key is missing. Set GEMINI_API_KEY in the environment.")

        if self._client is None:
            try:
                from google import genai
                from google.genai import types

                self._client = genai.Client(
                    api_key=self.settings.gemini_api_key,
                    http_options=types.HttpOptions(
                        timeout=self.settings.gemini_timeout_ms,
                        # Avoid serial timeouts on unreachable IPv6 addresses.
                        client_args={
                            "transport": httpx.HTTPTransport(
                                local_address="0.0.0.0"
                            )
                        },
                    ),
                )
            except GeminiError:
                raise
            except Exception as exc:
                log.exception("gemini_client_init_failed")
                raise GeminiError("Gemini client could not be initialized.") from exc

        return self._client

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        client = self._ensure_client()
        vectors: list[list[float]] = []
        batch = self.settings.embed_batch_size

        try:
            for i in range(0, len(texts), batch):
                chunk = texts[i : i + batch]

                log.info(
                    "embedding_generation count=%s model=%s",
                    len(chunk),
                    self.settings.embedding_model,
                )

                response = client.models.embed_content(
                    model=self.settings.embedding_model,
                    contents=chunk,
                )

                embeddings = getattr(response, "embeddings", None) or []

                if len(embeddings) != len(chunk):
                    raise EmbeddingError(
                        "Embedding service returned an unexpected result."
                    )

                for item in embeddings:
                    values = list(getattr(item, "values", None) or [])

                    if not values:
                        raise EmbeddingError(
                            "Embedding service returned an empty vector."
                        )

                    vectors.append(values)

        except EmbeddingError:
            raise
        except GeminiError:
            raise
        except Exception as exc:
            log.exception("embedding_failed")
            raise EmbeddingError("Failed to generate embeddings.") from exc

        return vectors

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        from google.genai import types

        client = self._ensure_client()

        try:
            response = client.models.generate_content(
                model=self.settings.gemini_model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )

            text = (getattr(response, "text", None) or "").strip()

            if not text:
                raise GeminiError("Gemini returned an empty response.")

            return _parse_json_object(text)

        except GeminiError:
            raise
        except Exception as exc:
            message = str(exc).lower()

            if (
                "api key" in message
                or "permission" in message
                or "401" in message
                or "403" in message
            ):
                log.error("gemini_auth_failed")
                raise GeminiError(
                    "Gemini rejected the API key or is unavailable."
                ) from exc

            log.exception("gemini_generate_failed")
            raise GeminiError("Gemini is currently unavailable.") from exc

    def generate_text(self, system: str, user: str) -> str:
        from google.genai import types

        client = self._ensure_client()

        try:
            response = client.models.generate_content(
                model=self.settings.gemini_model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.1,
                ),
            )

            text = (getattr(response, "text", None) or "").strip()

            if not text:
                raise GeminiError("Gemini returned an empty response.")

            return text

        except GeminiError:
            raise
        except Exception as exc:
            message = str(exc).lower()

            if (
                "api key" in message
                or "permission" in message
                or "401" in message
                or "403" in message
            ):
                log.error("gemini_auth_failed")
                raise GeminiError(
                    "Gemini rejected the API key or is unavailable."
                ) from exc

            log.exception("gemini_generate_text_failed")
            raise GeminiError("Gemini is currently unavailable.") from exc


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)

        if isinstance(data, dict):
            return data

    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)

        if match:
            data = json.loads(match.group(0))

            if isinstance(data, dict):
                return data

    raise GeminiError("Gemini returned invalid structured output.")
