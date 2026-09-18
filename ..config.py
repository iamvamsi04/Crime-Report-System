from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # ------------------------------------------------------------------
    # Gemini
    # ------------------------------------------------------------------

    gemini_api_key: str
    gemini_model: str
    embedding_model: str
    embedding_dimensions: int

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------

    host: str
    port: int

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    sqlite_path: Path
    chroma_path: Path
    upload_path: Path

    # ------------------------------------------------------------------
    # Text chunking / retrieval
    # ------------------------------------------------------------------

    chunk_size: int
    chunk_overlap: int
    retrieval_top_k: int
    retrieval_min_similarity: float

    # ------------------------------------------------------------------
    # Upload / embeddings
    # ------------------------------------------------------------------

    max_upload_bytes: int
    embed_batch_size: int

    # ------------------------------------------------------------------
    # External-service timeout
    # ------------------------------------------------------------------

    request_timeout_seconds: float

    # ------------------------------------------------------------------
    # Dynamic structured analysis
    # ------------------------------------------------------------------

    # Number of times Gemini may repair generated SQL after the initial
    # validation/execution attempt fails.
    analysis_max_repair_attempts: int

    # Maximum number of result rows exposed to the final answer model and
    # frontend. DuckDB still computes the complete query; this bounds the
    # materialized evidence sent downstream.
    analysis_max_result_rows: int


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    """
    Load application settings from environment variables.

    Existing deployments continue to work without adding new environment
    variables because every new structured-analysis setting has a default.
    """

    base_dir = Path(
        os.getenv(
            "APP_DATA_DIR",
            "data",
        )
    ).expanduser().resolve()

    sqlite_path = Path(
        os.getenv(
            "SQLITE_PATH",
            str(
                base_dir
                / "app.db"
            ),
        )
    ).expanduser().resolve()

    chroma_path = Path(
        os.getenv(
            "CHROMA_PATH",
            str(
                base_dir
                / "chroma"
            ),
        )
    ).expanduser().resolve()

    upload_path = Path(
        os.getenv(
            "UPLOAD_PATH",
            str(
                base_dir
                / "uploads"
            ),
        )
    ).expanduser().resolve()

    settings = Settings(
        gemini_api_key=os.getenv(
            "GEMINI_API_KEY",
            "",
        ).strip(),

        gemini_model=os.getenv(
            "GEMINI_MODEL",
            "gemini-3.6-flash",
        ).strip(),

        embedding_model=os.getenv(
            "EMBEDDING_MODEL",
            "gemini-embedding-001",
        ).strip(),

        embedding_dimensions=_env_int(
            "EMBEDDING_DIMENSIONS",
            768,
            minimum=1,
        ),

        host=os.getenv(
            "HOST",
            "127.0.0.1",
        ).strip(),

        port=_env_int(
            "PORT",
            8000,
            minimum=1,
            maximum=65535,
        ),

        sqlite_path=sqlite_path,
        chroma_path=chroma_path,
        upload_path=upload_path,

        chunk_size=_env_int(
            "CHUNK_SIZE",
            1200,
            minimum=100,
        ),

        chunk_overlap=_env_int(
            "CHUNK_OVERLAP",
            200,
            minimum=0,
        ),

        retrieval_top_k=_env_int(
            "RETRIEVAL_TOP_K",
            8,
            minimum=1,
        ),

        retrieval_min_similarity=_env_float(
            "RETRIEVAL_MIN_SIMILARITY",
            0.20,
            minimum=0.0,
            maximum=1.0,
        ),

        max_upload_bytes=_env_int(
            "MAX_UPLOAD_BYTES",
            25 * 1024 * 1024,
            minimum=1,
        ),

        embed_batch_size=_env_int(
            "EMBED_BATCH_SIZE",
            32,
            minimum=1,
        ),

        request_timeout_seconds=_env_float(
            "REQUEST_TIMEOUT_SECONDS",
            45.0,
            minimum=1.0,
        ),

        analysis_max_repair_attempts=_env_int(
            "ANALYSIS_MAX_REPAIR_ATTEMPTS",
            2,
            minimum=0,
            maximum=10,
        ),

        analysis_max_result_rows=_env_int(
            "ANALYSIS_MAX_RESULT_ROWS",
            200,
            minimum=1,
            maximum=10_000,
        ),
    )

    _ensure_directories(
        settings
    )

    return settings


def _ensure_directories(
    settings: Settings,
) -> None:
    settings.sqlite_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    settings.chroma_path.mkdir(
        parents=True,
        exist_ok=True,
    )

    settings.upload_path.mkdir(
        parents=True,
        exist_ok=True,
    )


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.getenv(
        name
    )

    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(
                raw.strip()
            )
        except ValueError as exc:
            raise ValueError(
                f"{name} must be an integer."
            ) from exc

    if (
        minimum is not None
        and value < minimum
    ):
        raise ValueError(
            f"{name} must be >= {minimum}."
        )

    if (
        maximum is not None
        and value > maximum
    ):
        raise ValueError(
            f"{name} must be <= {maximum}."
        )

    return value


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.getenv(
        name
    )

    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(
                raw.strip()
            )
        except ValueError as exc:
            raise ValueError(
                f"{name} must be a number."
            ) from exc

    if (
        minimum is not None
        and value < minimum
    ):
        raise ValueError(
            f"{name} must be >= {minimum}."
        )

    if (
        maximum is not None
        and value > maximum
    ):
        raise ValueError(
            f"{name} must be <= {maximum}."
        )

    return value
