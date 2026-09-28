from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import chromadb
import pymupdf
from google import genai
from google.genai import types
from langchain_text_splitters import RecursiveCharacterTextSplitter

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





BATCH_SIZE = 50

MIN_SECONDS_BETWEEN_REQUESTS = 0.7

MAX_RETRIES = 6
INITIAL_RETRY_DELAY = 2.0
MAX_RETRY_DELAY = 30.0


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



_last_embedding_request_time = 0.0


def _wait_before_embedding_request() -> None:
    """
    Keep embedding requests spaced out so we do not
    continuously hit the Gemini RPM limit.
    """

    global _last_embedding_request_time

    now = time.monotonic()

    elapsed = (
        now - _last_embedding_request_time
    )

    remaining = (
        MIN_SECONDS_BETWEEN_REQUESTS - elapsed
    )

    if remaining > 0:
        time.sleep(remaining)

    _last_embedding_request_time = (
        time.monotonic()
    )



def extract_pdf_pages(
    path: Path,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []

    with pymupdf.open(path) as pdf:
        for page_number, page in enumerate(
            pdf,
            start=1,
        ):
            text = page.get_text(
                "text"
            ).strip()

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
                    "page": page_data.get(
                        "page"
                    ),
                    "section": page_data.get(
                        "section"
                    ),
                    "chunk_index": chunk_index,
                }
            )

    return chunks



def _prepare_document_content(
    text: str,
    title: str | None = None,
) -> types.Content:
    """
    Gemini Embedding 2 retrieval document format.

    Using a Content object ensures each input receives
    its own embedding when multiple contents are supplied.
    """

    if not title:
        title = "none"

    formatted_text = (
        f"title: {title} | "
        f"text: {text}"
    )

    return types.Content(
        parts=[
            types.Part.from_text(
                text=formatted_text
            )
        ]
    )


def _prepare_query_content(
    query: str,
) -> types.Content:
    """
    Gemini Embedding 2 retrieval query format.
    """

    formatted_query = (
        "task: question answering | "
        f"query: {query}"
    )

    return types.Content(
        parts=[
            types.Part.from_text(
                text=formatted_query
            )
        ]
    )


def _is_resource_exhausted(
    error: Exception,
) -> bool:
    """
    Detect Gemini rate/quota exhaustion errors.
    """

    message = str(error).upper()

    return (
        "RESOURCE_EXHAUSTED" in message
        or "429" in message
        or "TOO MANY REQUESTS" in message
        or "RATE LIMIT" in message
    )


def _embed_batch(
    contents: list[types.Content],
) -> list[list[float]]:
    """
    Embed one batch with retry/backoff.

    Gemini Embedding 2 returns a separate embedding
    for each Content object in the contents list.
    """

    retry_delay = INITIAL_RETRY_DELAY

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):
        try:
            _wait_before_embedding_request()

            response = _client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=contents,
            )

            embeddings: list[list[float]] = []

            for embedding in (
                response.embeddings or []
            ):
                embeddings.append(
                    list(embedding.values)
                )

            if len(embeddings) != len(
                contents
            ):
                raise RuntimeError(
                    "Gemini returned "
                    f"{len(embeddings)} embeddings "
                    f"for {len(contents)} inputs."
                )

            return embeddings

        except Exception as exc:
            if not _is_resource_exhausted(
                exc
            ):
                raise

            if attempt >= MAX_RETRIES:
                log.error(
                    "Gemini embedding failed after "
                    "%s attempts.",
                    MAX_RETRIES,
                )
                raise

            log.warning(
                "Gemini RESOURCE_EXHAUSTED "
                "(attempt %s/%s). "
                "Retrying in %.1f seconds...",
                attempt,
                MAX_RETRIES,
                retry_delay,
            )

            time.sleep(retry_delay)

            retry_delay = min(
                retry_delay * 2,
                MAX_RETRY_DELAY,
            )

    raise RuntimeError(
        "Embedding request failed."
    )


def generate_embeddings(
    texts: list[str],
    task_type: str,
    title: str | None = None,
) -> list[list[float]]:
    """
    Generate one Gemini Embedding 2 vector per text.

    task_type is retained in the function signature so
    existing callers continue to work.

    Gemini Embedding 2 does not use the old
    task_type parameter. Retrieval instructions are
    encoded directly into the input content instead.
    """

    if not texts:
        return []

    embeddings: list[list[float]] = []

    total_batches = (
        len(texts) + BATCH_SIZE - 1
    ) // BATCH_SIZE

    log.info(
        "Embedding %s texts using %s "
        "in %s batches.",
        len(texts),
        EMBEDDING_MODEL,
        total_batches,
    )

    for batch_number, start in enumerate(
        range(
            0,
            len(texts),
            BATCH_SIZE,
        ),
        start=1,
    ):
        batch = texts[
            start : start + BATCH_SIZE
        ]

        log.info(
            "Embedding batch %s/%s "
            "(texts %s-%s).",
            batch_number,
            total_batches,
            start + 1,
            min(
                start + BATCH_SIZE,
                len(texts),
            ),
        )

        if task_type == (
            "RETRIEVAL_DOCUMENT"
        ):
            contents = [
                _prepare_document_content(
                    text,
                    title=title,
                )
                for text in batch
            ]

        elif task_type == (
            "RETRIEVAL_QUERY"
        ):
            contents = [
                _prepare_query_content(
                    text
                )
                for text in batch
            ]

        else:
            contents = [
                types.Content(
                    parts=[
                        types.Part.from_text(
                            text=text
                        )
                    ]
                )
                for text in batch
            ]

        batch_embeddings = _embed_batch(
            contents
        )

        embeddings.extend(
            batch_embeddings
        )

        log.info(
            "Completed embedding batch "
            "%s/%s.",
            batch_number,
            total_batches,
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

    log.info(
        "Document %s (%s) produced %s chunks.",
        document.id,
        document.filename,
        len(chunks),
    )

    total_batches = (
        len(chunks) + BATCH_SIZE - 1
    ) // BATCH_SIZE

    log.info(
        "Document will be processed in "
        "%s embedding batches.",
        total_batches,
    )


    all_ids: list[str] = []
    all_metadatas: list[
        dict[str, Any]
    ] = []

    for index, chunk in enumerate(
        chunks
    ):
        chunk_id = (
            f"{document.id}:{index}"
        )

        all_ids.append(chunk_id)

        metadata = {
            "document_id": document.id,
            "filename": document.filename,
            "file_type": document.file_type,
            "chunk_id": chunk_id,
        }

        if chunk["page"] is not None:
            metadata["page"] = chunk[
                "page"
            ]

        if chunk["section"] is not None:
            metadata["section"] = chunk[
                "section"
            ]

        all_metadatas.append(
            metadata
        )


    for start in range(
        0,
        len(chunks),
        BATCH_SIZE,
    ):
        end = min(
            start + BATCH_SIZE,
            len(chunks),
        )

        batch_chunks = chunks[
            start:end
        ]

        batch_ids = all_ids[
            start:end
        ]

        batch_metadatas = (
            all_metadatas[
                start:end
            ]
        )


        existing = _collection.get(
            ids=batch_ids,
            include=[],
        )

        existing_ids = set(
            existing.get("ids") or []
        )

        missing_indexes: list[int] = []

        for local_index, chunk_id in enumerate(
            batch_ids
        ):
            if chunk_id not in existing_ids:
                missing_indexes.append(
                    local_index
                )

        if not missing_indexes:
            log.info(
                "Batch %s-%s already exists "
                "in Chroma. Skipping.",
                start + 1,
                end,
            )
            continue

        texts_to_embed = [
            batch_chunks[index][
                "content"
            ]
            for index in missing_indexes
        ]

        ids_to_add = [
            batch_ids[index]
            for index in missing_indexes
        ]

        metadatas_to_add = [
            batch_metadatas[index]
            for index in missing_indexes
        ]

        log.info(
            "Processing chunks %s-%s "
            "(%s new chunks).",
            start + 1,
            end,
            len(texts_to_embed),
        )

        embeddings = generate_embeddings(
            texts_to_embed,
            task_type="RETRIEVAL_DOCUMENT",
            title=document.filename,
        )

        if len(embeddings) != len(
            texts_to_embed
        ):
            raise RuntimeError(
                "Embedding count does not match "
                "chunk count."
            )


        _collection.add(
            ids=ids_to_add,
            embeddings=embeddings,
            documents=texts_to_embed,
            metadatas=metadatas_to_add,
        )

        log.info(
            "Stored chunks %s-%s in Chroma.",
            start + 1,
            end,
        )

    log.info(
        "Finished indexing document %s "
        "(%s).",
        document.id,
        document.filename,
    )



def embed_query(
    query: str,
) -> list[float]:
    contents = [
        _prepare_query_content(
            query
        )
    ]

    embeddings = _embed_batch(
        contents
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
        results.get("documents")
        or [[]]
    )[0]

    metadatas = (
        results.get("metadatas")
        or [[]]
    )[0]

    distances = (
        results.get("distances")
        or [[]]
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

        if (
            similarity
            < RAG_MIN_SIMILARITY
        ):
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
                    metadata.get(
                        "section"
                    )
                ),
                chunk_id=_optional_string(
                    metadata.get(
                        "chunk_id"
                    )
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
    except (
        TypeError,
        ValueError,
    ):
        return None


def _optional_string(
    value: Any,
) -> str | None:
    if value is None:
        return None

    value = str(value).strip()

    return value or None
