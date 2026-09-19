from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

from google import genai
from google.genai import types

from app.config import Settings
from app.errors import GeminiError


log = logging.getLogger(__name__)


@runtime_checkable
class GeminiService(Protocol):
    """
    Minimal Gemini interface used by the rest of the application.

    Keeping the application dependent on this interface rather than directly
    on the Google SDK makes the planner, retriever, analyzer, and chat layer
    easier to test and keeps Gemini-specific code in one place.
    """

    def generate(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> str:
        ...

    def generate_json(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        ...

    def embed_texts(
        self,
        texts: list[str],
        *,
        task_type: str | None = None,
    ) -> list[list[float]]:
        ...


class GeminiClient:
    """
    Synchronous Gemini client used by the application.

    Responsibilities:
        - text generation
        - structured JSON generation
        - text embeddings

    No application-specific planning, SQL generation, retrieval logic, or
    answer-generation rules belong here. Those are handled by their
    respective application layers.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        if not settings.gemini_api_key:
            raise GeminiError(
                "GEMINI_API_KEY is not configured."
            )

        try:
            self.client = genai.Client(
                api_key=settings.gemini_api_key,
            )
        except Exception as exc:
            log.exception("gemini_client_initialization_failed")
            raise GeminiError(
                "Failed to initialize the Gemini client."
            ) from exc

    def generate(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> str:
        """
        Generate plain text from Gemini.

        The application uses this for tasks such as:
            - final grounded answers
            - query interpretation
            - other text-only generation
        """

        if not prompt or not prompt.strip():
            raise GeminiError(
                "Gemini generation requires a non-empty prompt."
            )

        config_kwargs: dict[str, Any] = {
            "temperature": temperature,
        }

        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction

        if max_output_tokens is not None:
            config_kwargs["max_output_tokens"] = max_output_tokens

        try:
            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    **config_kwargs,
                ),
            )
        except Exception as exc:
            log.exception(
                "gemini_generate_failed model=%s",
                self.settings.gemini_model,
            )
            raise GeminiError(
                "Gemini generation failed."
            ) from exc

        text = self._extract_text(response)

        if not text:
            raise GeminiError(
                "Gemini returned an empty response."
            )

        return text

    def generate_json(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """
        Generate and parse a JSON object from Gemini.

        This is intentionally a generic JSON interface. The planner and
        analyzer validate the returned structure against their own models.
        """

        if not prompt or not prompt.strip():
            raise GeminiError(
                "Gemini JSON generation requires a non-empty prompt."
            )

        config_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "response_mime_type": "application/json",
        }

        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction

        if max_output_tokens is not None:
            config_kwargs["max_output_tokens"] = max_output_tokens

        try:
            response = self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    **config_kwargs,
                ),
            )
        except Exception as exc:
            log.exception(
                "gemini_generate_json_failed model=%s",
                self.settings.gemini_model,
            )
            raise GeminiError(
                "Gemini JSON generation failed."
            ) from exc

        text = self._extract_text(response)

        if not text:
            raise GeminiError(
                "Gemini returned an empty JSON response."
            )

        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            log.error(
                "gemini_invalid_json response=%r",
                text[:1000],
            )
            raise GeminiError(
                "Gemini returned invalid JSON."
            ) from exc

        if not isinstance(value, dict):
            raise GeminiError(
                "Gemini JSON response must be an object."
            )

        return value

    def embed_texts(
        self,
        texts: list[str],
        *,
        task_type: str | None = None,
    ) -> list[list[float]]:
        """
        Generate embeddings for multiple text chunks.

        Embeddings are requested in batches so a large document does not
        become one enormous API request.

        The returned list has exactly one embedding for every non-empty
        input text.
        """

        if not texts:
            return []

        cleaned: list[str] = []

        for text in texts:
            if text is None:
                continue

            value = str(text).strip()

            if value:
                cleaned.append(value)

        if not cleaned:
            return []

        batch_size = max(
            1,
            self.settings.embed_batch_size,
        )

        all_embeddings: list[list[float]] = []

        for start in range(0, len(cleaned), batch_size):
            batch = cleaned[
                start : start + batch_size
            ]

            embeddings = self._embed_batch(
                batch,
                task_type=task_type,
            )

            all_embeddings.extend(embeddings)

        if len(all_embeddings) != len(cleaned):
            raise GeminiError(
                "Gemini returned an unexpected number of embeddings."
            )

        return all_embeddings

    def _embed_batch(
        self,
        texts: list[str],
        *,
        task_type: str | None = None,
    ) -> list[list[float]]:
        """
        Embed one batch of texts.
        """

        config_kwargs: dict[str, Any] = {
            "output_dimensionality": self.settings.embedding_dimensions,
        }

        if task_type:
            config_kwargs["task_type"] = task_type

        try:
            response = self.client.models.embed_content(
                model=self.settings.embedding_model,
                contents=texts,
                config=types.EmbedContentConfig(
                    **config_kwargs,
                ),
            )
        except Exception as exc:
            log.exception(
                "gemini_embedding_failed model=%s batch_size=%d",
                self.settings.embedding_model,
                len(texts),
            )
            raise GeminiError(
                "Gemini embedding generation failed."
            ) from exc

        raw_embeddings = getattr(
            response,
            "embeddings",
            None,
        )

        if not raw_embeddings:
            raise GeminiError(
                "Gemini returned no embeddings."
            )

        result: list[list[float]] = []

        for item in raw_embeddings:
            values = getattr(
                item,
                "values",
                None,
            )

            if values is None:
                if isinstance(item, dict):
                    values = item.get("values")

            if not values:
                raise GeminiError(
                    "Gemini returned an invalid embedding."
                )

            vector = [
                float(value)
                for value in values
            ]

            result.append(vector)

        return result

    @staticmethod
    def _extract_text(response: Any) -> str:
        """
        Extract response text safely from a Google GenAI response.

        The SDK normally exposes response.text, but this method also handles
        responses where text is unavailable and the candidate parts need to
        be inspected.
        """

        text = getattr(
            response,
            "text",
            None,
        )

        if isinstance(text, str) and text.strip():
            return text.strip()

        candidates = getattr(
            response,
            "candidates",
            None,
        )

        if not candidates:
            return ""

        parts_text: list[str] = []

        for candidate in candidates:
            content = getattr(
                candidate,
                "content",
                None,
            )

            if content is None:
                continue

            parts = getattr(
                content,
                "parts",
                None,
            )

            if not parts:
                continue

            for part in parts:
                part_text = getattr(
                    part,
                    "text",
                    None,
                )

                if isinstance(part_text, str) and part_text.strip():
                    parts_text.append(
                        part_text.strip()
                    )

        return "\n".join(parts_text).strip()
