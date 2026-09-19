from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()

    if path.is_absolute():
        return path

    return BASE_DIR / path


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"Environment variable {name!r} must be an integer."
        ) from exc


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(
            f"Environment variable {name!r} must be a number."
        ) from exc


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str
    embedding_model: str

    app_host: str
    app_port: int

    sqlite_path: Path
    chroma_path: Path
    upload_dir: Path

    chunk_size: int
    chunk_overlap: int

    retrieval_top_k: int
    retrieval_min_similarity: float

    max_upload_bytes: int

    embed_batch_size: int
    gemini_timeout_ms: int

    embedding_dimensions: int

    def ensure_directories(self) -> None:
        """
        Create all directories required by the application.

        SQLite itself is a file, so only its parent directory needs to
        exist. Chroma and uploaded documents use directories directly.
        """

        self.sqlite_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.chroma_path.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.upload_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
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

            app_host=os.getenv(
                "APP_HOST",
                "127.0.0.1",
            ).strip(),

            app_port=_env_int(
                "APP_PORT",
                8000,
            ),

            sqlite_path=_resolve_path(
                os.getenv(
                    "SQLITE_PATH",
                    "data/app.sqlite3",
                )
            ),

            chroma_path=_resolve_path(
                os.getenv(
                    "CHROMA_PATH",
                    "data/chroma",
                )
            ),

            upload_dir=_resolve_path(
                os.getenv(
                    "UPLOAD_DIR",
                    "data/uploads",
                )
            ),

            chunk_size=_env_int(
                "CHUNK_SIZE",
                900,
            ),

            chunk_overlap=_env_int(
                "CHUNK_OVERLAP",
                150,
            ),

            retrieval_top_k=_env_int(
                "RETRIEVAL_TOP_K",
                8,
            ),

            retrieval_min_similarity=_env_float(
                "RETRIEVAL_MIN_SIMILARITY",
                0.30,
            ),

            max_upload_bytes=_env_int(
                "MAX_UPLOAD_BYTES",
                20 * 1024 * 1024,
            ),

            embed_batch_size=_env_int(
                "EMBED_BATCH_SIZE",
                32,
            ),

            gemini_timeout_ms=_env_int(
                "GEMINI_TIMEOUT_MS",
                45_000,
            ),

            embedding_dimensions=_env_int(
                "EMBEDDING_DIMENSIONS",
                768,
            ),
        )


def load_settings() -> Settings:
    """
    Load application configuration from environment variables.

    The returned settings object is also responsible for creating the
    directories required by the application.
    """

    settings = Settings.from_env()
    settings.ensure_directories()

    return settings


def setup_logging() -> None:
    """
    Configure application-wide logging.

    This function is intentionally idempotent so that calling it during
    FastAPI startup does not create duplicate handlers.
    """

    level_name = os.getenv(
        "LOG_LEVEL",
        "INFO",
    ).strip().upper()

    level = getattr(
        logging,
        level_name,
        logging.INFO,
    )

    logging.basicConfig(
        level=level,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(name)s | "
            "%(message)s"
        ),
        force=False,
    )
