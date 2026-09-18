from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Protocol, runtime_checkable

from app.config import Settings
from app.errors import (
    EmbeddingError,
    GeminiError,
)

log = logging.getLogger(__name__)


@runtime_checkable
class GeminiService(Protocol):
    """
    Minimal interface consumed by the application.

    Keeping the rest of the backend dependent on this protocol rather than
    the Google SDK makes the planner/analyzer/retriever easy to test with a
    deterministic fake implementation.
    """

    def embed_texts(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        ...

    def generate_json(
        self,
        *,
        system: str,
        user: str,
    ) -> dict[str, Any]:
        ...


class GeminiClient:
    """
    Thin synchronous wrapper around the Google Gen AI SDK.

    The client is created lazily so importing the application does not require
    a network connection or immediately initialize the external SDK.
    """

    def __init__(
        self,
        settings: Settings,
    ) -> None:
        self.settings = settings
        self._client: Any | None = None

    @property
    def client(
        self,
    ) -> Any:
        if self._client is None:
            self._client = (
                self._create_client()
            )

        return self._client

    def embed_texts(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        """
        Embed texts in bounded batches.

        Returned vectors preserve the same order as the input texts.
        """

        cleaned = [
            str(text)
            for text in texts
        ]

        if not cleaned:
            return []

        batch_size = max(
            1,
            int(
                self.settings.embed_batch_size
            ),
        )

        vectors: list[
            list[float]
        ] = []

        for start in range(
            0,
            len(cleaned),
            batch_size,
        ):
            batch = cleaned[
                start:start + batch_size
            ]

            batch_vectors = (
                self._embed_batch(
                    batch
                )
            )

            if len(
                batch_vectors
            ) != len(batch):
                raise EmbeddingError(
                    "Embedding service returned an unexpected number of vectors."
                )

            vectors.extend(
                batch_vectors
            )

        return vectors

    def generate_json(
        self,
        *,
        system: str,
        user: str,
    ) -> dict[str, Any]:
        """
        Generate one JSON object.

        Planner, SQL generator, SQL repair, and final answer generation all
        use this method. Business logic and numerical computation remain
        outside this wrapper.
        """

        if not user.strip():
            raise GeminiError(
                "The model request was empty."
            )

        try:
            response = (
                self.client.models.generate_content(
                    model=(
                        self.settings.gemini_model
                    ),
                    contents=user,
                    config={
                        "system_instruction": (
                            system
                        ),
                        "temperature": 0.1,
                        "response_mime_type": (
                            "application/json"
                        ),
                    },
                )
            )

        except Exception as exc:
            log.exception(
                "gemini_generation_failed"
            )

            raise GeminiError(
                "The language model request failed."
            ) from exc

        text = _response_text(
            response
        )

        if not text:
            raise GeminiError(
                "The language model returned an empty response."
            )

        try:
            payload = _parse_json_object(
                text
            )
        except Exception as exc:
            log.warning(
                "gemini_invalid_json response=%s",
                _log_preview(text),
            )

            raise GeminiError(
                "The language model returned invalid JSON."
            ) from exc

        return payload

    def _embed_batch(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        if not texts:
            return []

        try:
            response = (
                self.client.models.embed_content(
                    model=(
                        self.settings.embedding_model
                    ),
                    contents=texts,
                    config={
                        "output_dimensionality": (
                            self.settings.embedding_dimensions
                        ),
                    },
                )
            )

        except Exception as exc:
            log.exception(
                "gemini_embedding_failed"
            )

            raise EmbeddingError(
                "The embedding request failed."
            ) from exc

        embeddings = getattr(
            response,
            "embeddings",
            None,
        )

        if embeddings is None:
            raise EmbeddingError(
                "The embedding service returned no embeddings."
            )

        vectors: list[
            list[float]
        ] = []

        for embedding in embeddings:
            values = getattr(
                embedding,
                "values",
                None,
            )

            if values is None:
                raise EmbeddingError(
                    "The embedding service returned an invalid vector."
                )

            vector = [
                float(value)
                for value in values
            ]

            if not vector:
                raise EmbeddingError(
                    "The embedding service returned an empty vector."
                )

            vectors.append(
                vector
            )

        return vectors

    def _create_client(
        self,
    ) -> Any:
        api_key = (
            self.settings.gemini_api_key
            .strip()
        )

        if not api_key:
            raise GeminiError(
                "GEMINI_API_KEY is not configured."
            )

        try:
            from google import genai
        except ImportError as exc:
            raise GeminiError(
                "The Google Gen AI SDK is not installed."
            ) from exc

        try:
            return genai.Client(
                api_key=api_key
            )
        except Exception as exc:
            raise GeminiError(
                "The Gemini client could not be initialized."
            ) from exc


def _response_text(
    response: Any,
) -> str:
    """
    Extract response text defensively across small SDK response-shape changes.
    """

    direct = getattr(
        response,
        "text",
        None,
    )

    if isinstance(
        direct,
        str,
    ) and direct.strip():
        return direct.strip()

    candidates = getattr(
        response,
        "candidates",
        None,
    )

    if not candidates:
        return ""

    pieces: list[str] = []

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
            text = getattr(
                part,
                "text",
                None,
            )

            if isinstance(
                text,
                str,
            ) and text:
                pieces.append(
                    text
                )

    return "\n".join(
        pieces
    ).strip()


def _parse_json_object(
    text: str,
) -> dict[str, Any]:
    """
    Parse model output as one JSON object.

    response_mime_type normally gives clean JSON, but the fallback handling
    tolerates fenced output or a small amount of accidental surrounding text.
    """

    candidate = (
        text.strip()
    )

    candidate = _strip_code_fence(
        candidate
    )

    try:
        parsed = json.loads(
            candidate
        )

        if not isinstance(
            parsed,
            dict,
        ):
            raise ValueError(
                "Expected a JSON object."
            )

        return parsed

    except json.JSONDecodeError:
        pass

    extracted = _extract_json_object(
        candidate
    )

    if extracted is None:
        raise ValueError(
            "No JSON object was found."
        )

    parsed = json.loads(
        extracted
    )

    if not isinstance(
        parsed,
        dict,
    ):
        raise ValueError(
            "Expected a JSON object."
        )

    return parsed


def _strip_code_fence(
    text: str,
) -> str:
    match = re.fullmatch(
        r"""
        \s*
        ```(?:json)?
        \s*
        (?P<body>.*?)
        \s*
        ```
        \s*
        """,
        text,
        flags=(
            re.IGNORECASE
            | re.DOTALL
            | re.VERBOSE
        ),
    )

    if not match:
        return text

    return match.group(
        "body"
    ).strip()


def _extract_json_object(
    text: str,
) -> str | None:
    """
    Find the first balanced top-level JSON object while respecting quoted
    strings and escaped characters.
    """

    start = text.find(
        "{"
    )

    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(
        start,
        len(text),
    ):
        character = text[
            index
        ]

        if in_string:
            if escaped:
                escaped = False
                continue

            if character == "\\":
                escaped = True
                continue

            if character == '"':
                in_string = False

            continue

        if character == '"':
            in_string = True
            continue

        if character == "{":
            depth += 1
            continue

        if character == "}":
            depth -= 1

            if depth == 0:
                return text[
                    start:index + 1
                ]

    return None


def _log_preview(
    text: str,
    limit: int = 500,
) -> str:
    """
    Keep logs useful without dumping large model responses.
    """

    compact = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    if len(compact) <= limit:
        return compact

    return (
        compact[:limit]
        + "…"
    )
