from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return BASE_DIR / path


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

    embed_batch_size: int = 32
    gemini_timeout_ms: int = 45000
    embedding_dimensions: int = 768

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
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
            app_port=int(
                os.getenv(
                    "APP_PORT",
                    "8000",
                )
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
            chunk_size=int(
                os.getenv(
                    "CHUNK_SIZE",
                    "900",
                )
            ),
            chunk_overlap=int(
                os.getenv(
                    "CHUNK_OVERLAP",
                    "150",
                )
            ),
            retrieval_top_k=int(
                os.getenv(
                    "RETRIEVAL_TOP_K",
                    "8",
                )
            ),
            retrieval_min_similarity=float(
                os.getenv(
                    "RETRIEVAL_MIN_SIMILARITY",
                    "0.30",
                )
            ),
            max_upload_bytes=int(
                os.getenv(
                    "MAX_UPLOAD_BYTES",
                    str(20 * 1024 * 1024),
                )
            ),
            embed_batch_size=int(
                os.getenv(
                    "EMBED_BATCH_SIZE",
                    "32",
                )
            ),
            gemini_timeout_ms=int(
                os.getenv(
                    "GEMINI_TIMEOUT_MS",
                    "45000",
                )
            ),
            embedding_dimensions=int(
                os.getenv(
                    "EMBEDDING_DIMENSIONS",
                    "768",
                )
            ),
        )

    def ensure_directories(self) -> None:
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
