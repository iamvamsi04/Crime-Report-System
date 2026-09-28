from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import chromadb
import  pymupdf
from langchain_text_splitters import RecursiveCharacterTextSplitter
from google import genai

from app.config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    EMBEDDING_MODEL,
    RAG_MIN_SIMILARITY,
    RAG_TOP_K,
    VECTOR_STORE_DIR,
)
from app.models import Document, RetrievedChunk

log = logging.getLogger(__name__)





_client = genai.Client()





_chroma_client = chromadb.PersistentClient(
    path=str(VECTOR_STORE_DIR)
)

_collection = _chroma_client.get_or_create_collection(
    name="document_embeddings",
    metadata={
        "hnsw:space": "cosine",
    },
)




def extract_pdf_pages(
    path: Path,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []

    with  pymupdf.open(path) as pdf:
        for page_number, page in enumerate(
            pdf,
            start=1,
        ):
            text = page.get_text("text").strip()

            if not text:
                continue

            pages.append(
                {
                    "text": text,
                    "page": page_number,
                    "section": None,
                }
            )

    return pages


def extract_txt(
    path: Path,
) -> list[dict[str, Any]]:
    text = path.read_text(
        encoding="utf-8",
        errors="replace",
    ).strip()

    if not text:
        return []

    return [
        {
            "text": text,
            "page": None,
            "section": None,
        }
    ]


def extract_document_text(
    document: Document,
) -> list[dict[str, Any]]:
    path = Path(document.path)

    if not path.exists():
        raise FileNotFoundError(
            f"Document file not found: {path}"
        )

    if document.file_type == "pdf":
        return extract_pdf_pages(path)

    if document.file_type == "txt":
        return extract_txt(path)

    raise ValueError(
        f"RAG does not support "
        f"{document.file_type} files."
    )




def create_chunks(
    pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=[
            "\n\n",
            "\n",
            ". ",
            " ",
            "",
        ],
    )

    chunks: list[dict[str, Any]] = []

    for page_data in pages:
        text = page_data["text"]

        page_chunks = splitter.split_text(
            text
        )

        for chunk_index, chunk in enumerate(
            page_chunks
        ):
            chunk = chunk.strip()

            if not chunk:
                continue

            chunks.append(
                {
                    "content": chunk,
                    "page": page_data.get("page"),
                    "section": page_data.get(
                        "section"
                    ),
                    "chunk_index": chunk_index,
                }
            )

    return chunks





def generate_embeddings(
    texts: list[str],
    task_type: str,
) -> list[list[float]]:
    if not texts:
        return []

    embeddings: list[list[float]] = []

    batch_size = 50

    for start in range(
        0,
        len(texts),
        batch_size,
    ):
        batch = texts[
            start : start + batch_size
        ]

        response = _client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=batch,
            config={
                "task_type": task_type,
            },
        )

        for embedding in response.embeddings:
            embeddings.append(
                list(embedding.values)
            )

    return embeddings





async def ingest_text_document(
    document: Document,
) -> None:
    if document.file_type not in {
        "pdf",
        "txt",
    }:
        raise ValueError(
            "Only PDF and TXT files can be "
            "ingested into RAG."
        )

    pages = extract_document_text(
        document
    )

    if not pages:
        raise ValueError(
            "The document contains no readable text."
        )

    chunks = create_chunks(
        pages
    )

    if not chunks:
        raise ValueError(
            "No usable text chunks were created."
        )

    texts = [
        chunk["content"]
        for chunk in chunks
    ]

    embeddings = generate_embeddings(
        texts,
        task_type="RETRIEVAL_DOCUMENT",
    )

    if len(embeddings) != len(chunks):
        raise RuntimeError(
            "Embedding count does not match "
            "chunk count."
        )

    ids: list[str] = []
    metadatas: list[dict[str, Any]] = []

    for index, chunk in enumerate(
        chunks
    ):
        chunk_id = (
            f"{document.id}:{index}"
        )

        ids.append(chunk_id)

        metadata = {
            "document_id": document.id,
            "filename": document.filename,
            "file_type": document.file_type,
            "chunk_id": chunk_id,
        }

        if chunk["page"] is not None:
            metadata["page"] = chunk["page"]

        if chunk["section"] is not None:
            metadata["section"] = (
                chunk["section"]
            )

        metadatas.append(metadata)

    # Remove old vectors first so re-ingestion
    # does not leave stale chunks behind.
    delete_document_vectors(
        document.id
    )

    _collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=texts,
        metadatas=metadatas,
    )

    log.info(
        "Indexed %s chunks for %s",
        len(chunks),
        document.filename,
    )





def embed_query(
    query: str,
) -> list[float]:
    embeddings = generate_embeddings(
        [query],
        task_type="RETRIEVAL_QUERY",
    )

    if not embeddings:
        raise RuntimeError(
            "Failed to generate query embedding."
        )

    return embeddings[0]




def retrieve(
    query: str,
    document_ids: list[str] | None = None,
    top_k: int = RAG_TOP_K,
) -> list[RetrievedChunk]:
    query_embedding = embed_query(
        query
    )

    where: dict[str, Any] | None = None

    if document_ids:
        if len(document_ids) == 1:
            where = {
                "document_id": document_ids[0]
            }
        else:
            where = {
                "$or": [
                    {
                        "document_id": document_id
                    }
                    for document_id in document_ids
                ]
            }

    results = _collection.query(
        query_embeddings=[
            query_embedding
        ],
        n_results=top_k,
        where=where,
        include=[
            "documents",
            "metadatas",
            "distances",
        ],
    )

    documents = (
        results.get("documents") or [[]]
    )[0]

    metadatas = (
        results.get("metadatas") or [[]]
    )[0]

    distances = (
        results.get("distances") or [[]]
    )[0]

    retrieved: list[RetrievedChunk] = []

    for content, metadata, distance in zip(
        documents,
        metadatas,
        distances,
    ):
        # Chroma cosine distance:
        # similarity = 1 - distance
        similarity = 1.0 - float(
            distance
        )

        if similarity < RAG_MIN_SIMILARITY:
            continue

        retrieved.append(
            RetrievedChunk(
                document_id=str(
                    metadata.get(
                        "document_id",
                        "",
                    )
                ),
                filename=str(
                    metadata.get(
                        "filename",
                        "Unknown",
                    )
                ),
                content=str(content),
                score=similarity,
                page=_optional_int(
                    metadata.get("page")
                ),
                section=_optional_string(
                    metadata.get("section")
                ),
                chunk_id=_optional_string(
                    metadata.get("chunk_id")
                ),
            )
        )

    return retrieved





def delete_document_vectors(
    document_id: str,
) -> None:
    _collection.delete(
        where={
            "document_id": document_id
        }
    )

    log.info(
        "Deleted vectors for document %s",
        document_id,
    )





def _optional_int(
    value: Any,
) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_string(
    value: Any,
) -> str | None:
    if value is None:
        return None

    value = str(value).strip()

    return value or None
