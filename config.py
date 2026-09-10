"""Local application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Settings:
    """Paths and optional Gemini settings used by the application."""

    project_root: Path
    documents_dir: Path
    data_dir: Path
    upload_dir: Path
    chroma_dir: Path
    embedding_cache_dir: Path
    session_db_path: Path
    gemini_api_key: str | None
    gemini_model: str
    log_level: str
    embedding_model: str = "all-MiniLM-L6-v2"
    chroma_distance_space: str = "cosine"
    retrieval_min_similarity: float = 0.22
    retrieval_limit: int = 8

    @classmethod
    def from_environment(cls, project_root: Path | None = None) -> "Settings":
        """Read settings while keeping all persisted data local to this project."""
        root = project_root or PROJECT_ROOT
        load_dotenv(root / ".env")
        documents_dir = root / os.getenv("DOCUMENTS_DIR", "documents")
        data_dir = root / os.getenv("DATA_DIR", "data")
        return cls(
            project_root=root,
            documents_dir=documents_dir,
            data_dir=data_dir,
            upload_dir=data_dir / "uploads",
            chroma_dir=data_dir / "chroma",
            embedding_cache_dir=root / os.getenv("EMBEDDING_CACHE_DIR", str(Path("data") / "embedding-cache")),
            session_db_path=data_dir / "sessions.db",
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    def ensure_directories(self) -> None:
        """Create only the application's local data directories."""
        for directory in (
            self.documents_dir,
            self.data_dir,
            self.upload_dir,
            self.chroma_dir,
            self.embedding_cache_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings.from_environment()
