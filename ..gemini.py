from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol, runtime_checkable

import httpx
from google import genai
from google.genai import types

from app.config import Settings
from app.errors import EmbeddingError, GeminiError

log = logging.getLogger(__name__)


@runtime_checkable
class GeminiService(Protocol):
    """
    Minimal interface consumed by the application.

    Keeping the rest of the backend dependent on this protocol rather than
    the Google SDK makes the Gemini integration easier to test and replace.
    """

    def embed_texts(
        self,
        texts: list[str],
        *,
        task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> list[list[float]]:
        ...

    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        ...

    def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        ...


class GeminiClient:
    """
    Gemini client used by the application.

    The same client is used for:
      - document embeddings
      - planner JSON generation
      - DuckDB SQL generation
      - final answer generation
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        if not settings.gemini_api_key:
            raise GeminiError(
                "GEMINI_API_KEY is not configured."
            )

        timeout_seconds = max(
            settings.gemini_timeout_ms / 1000,
            1,
        )

        self._http_client = httpx.Client(
            timeout=timeout_seconds,
            transport=httpx.HTTPTransport(
                local_address="0.0.0.0",
            ),
        )

        try:
            self.client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(
                    timeout=settings.gemini_timeout_ms,
                ),
            )
        except Exception as exc:
            log.exception("Failed to initialize Gemini client")
            raise GeminiError(
                f"Failed to initialize Gemini client: {exc}"
            ) from exc

    def embed_texts(
        self,
        texts: list[str],
        *,
        task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> list[list[float]]:
        if not texts:
            return []

        embeddings: list[list[float]] = []

        batch_size = max(
            1,
            self.settings.embed_batch_size,
        )

        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]

            try:
                response = self.client.models.embed_content(
                    model=self.settings.embedding_model,
                    contents=batch,
                    config=types.EmbedContentConfig(
                        task_type=task_type,
                        output_dimensionality=(
                            self.settings.embedding_dimensions
                        ),
                    ),
                )

                values = getattr(response, "embeddings", None)

                if values is None:
                    raise EmbeddingError(
                        "Gemini returned no embeddings."
                    )

                for embedding in values:
                    vector = getattr(
                        embedding,
                        "values",
                        None,
                    )

                    if not vector:
                        raise EmbeddingError(
                            "Gemini returned an empty embedding."
                        )

                    embeddings.append(
                        [float(value) for value in vector]
                    )

            except EmbeddingError:
                raise
            except Exception as exc:
                log.exception(
                    "Gemini embedding request failed"
                )
                raise EmbeddingError(
                    f"Gemini embedding failed: {exc}"
                ) from exc

        if len(embeddings) != len(texts):
            raise EmbeddingError(
                "Gemini returned an unexpected number of embeddings."
            )

        return embeddings

    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        """
        Generate a JSON object from Gemini.

        This is also used by the DuckDB analysis layer. The SQL generator
        therefore does not need its own LLM client.
        """
        response = self._generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_mime_type="application/json",
        )

        text = self._response_text(response)

        if not text:
            raise GeminiError(
                "Gemini returned an empty JSON response."
            )

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            log.error(
                "Invalid JSON returned by Gemini: %s",
                text[:1000],
            )
            raise GeminiError(
                "Gemini returned invalid JSON."
            ) from exc

        if not isinstance(data, dict):
            raise GeminiError(
                "Gemini JSON response must be an object."
            )

        return data

    def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        response = self._generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_mime_type="text/plain",
        )

        text = self._response_text(response)

        if not text:
            raise GeminiError(
                "Gemini returned an empty response."
            )

        return text.strip()

    def _generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_mime_type: str,
    ) -> Any:
        started = time.perf_counter()

        try:
            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type=response_mime_type,
                    temperature=0,
                ),
            )

            elapsed = time.perf_counter() - started

            log.debug(
                "Gemini generation completed in %.2fs",
                elapsed,
            )

            return response

        except Exception as exc:
            elapsed = time.perf_counter() - started

            log.exception(
                "Gemini generation failed after %.2fs",
                elapsed,
            )

            raise GeminiError(
                f"Gemini generation failed: {exc}"
            ) from exc

    @staticmethod
    def _response_text(response: Any) -> str:
        """
        Extract text from the Google GenAI response while tolerating
        SDK response-shape differences.
        """
        text = getattr(response, "text", None)

        if isinstance(text, str):
            return text.strip()

        candidates = getattr(response, "candidates", None)

        if not candidates:
            return ""

        parts: list[str] = []

        for candidate in candidates:
            content = getattr(candidate, "content", None)

            if content is None:
                continue

            response_parts = getattr(
                content,
                "parts",
                None,
            )

            if not response_parts:
                continue

            for part in response_parts:
                part_text = getattr(
                    part,
                    "text",
                    None,
                )

                if isinstance(part_text, str):
                    parts.append(part_text)

        return "".join(parts).strip()
