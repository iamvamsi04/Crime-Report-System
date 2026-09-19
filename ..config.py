from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


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
      gemini_timeout_ms: int = 45_000
      embedding_dimensions: int = 768


def load_settings(env_file: Path | None = None) -> Settings:
      load_dotenv(env_file or ROOT / ".env", override=False)

      def _path(key: str, default: str) -> Path:
            raw = os.getenv(key, default)
            path = Path(raw)
            if not path.is_absolute():
                  path = ROOT / path
            return path

      return Settings(
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip() or "gemini-3.6-flash",
            embedding_model=os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001").strip()
            or "gemini-embedding-001",
            app_host=os.getenv("APP_HOST", "127.0.0.1"),
            app_port=int(os.getenv("APP_PORT", "8000")),
            sqlite_path=_path("SQLITE_PATH", "data/app.sqlite3"),
            chroma_path=_path("CHROMA_PATH", "data/chroma"),
            upload_dir=_path("UPLOAD_DIR", "data/uploads"),
            chunk_size=int(os.getenv("CHUNK_SIZE", "900")),
            chunk_overlap=int(os.getenv("CHUNK_OVERLAP", "150")),
            retrieval_top_k=int(os.getenv("RETRIEVAL_TOP_K", "8")),
            retrieval_min_similarity=float(os.getenv("RETRIEVAL_MIN_SIMILARITY", "0.30")),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))),
      )


def setup_logging(level: int = logging.INFO) -> None:
      logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
            force=True,
      )
      logging.getLogger("chromadb").setLevel(logging.WARNING)
      logging.getLogger("httpx").setLevel(logging.WARNING)

