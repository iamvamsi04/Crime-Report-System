# Crime-Report-System

ERROR app.gemini - gemini_embedding_failed
Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\gemini.py", line 207, in_embed_batch
    self.client.models.embed_content(
    ^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\gemini.py", line 64, in client
    self._create_client()
    ~~~~~~~~~~~~~~~~~~~^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\gemini.py", line 281, in_create_client
    raise GeminiError(
        "GEMINI_API_KEY is not configured."
    )
app.errors.GeminiError: GEMINI_API_KEY is not configured.
2026-09-18 19:27:20,045 ERROR app.ingest - document_ingestion_failed document_id=b4c770ec-0254-40aa-b4bc-e40ef76053f5 filename=2014_economy.txt.txt
Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\gemini.py", line 207, in_embed_batch
    self.client.models.embed_content(
    ^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\gemini.py", line 64, in client
    self._create_client()
    ~~~~~~~~~~~~~~~~~~~^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\gemini.py", line 281, in_create_client
    raise GeminiError(
        "GEMINI_API_KEY is not configured."
    )
app.errors.GeminiError: GEMINI_API_KEY is not configured.

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\ingest.py", line 165, iningest_bytes
    _embed_chunks(
    ~~~~~~~~~~~~~^
        chunks=chunks,
        ^^^^^^^^^^^^^^
    ...<2 lines>...
        settings=settings,
        ^^^^^^^^^^^^^^^^^^
    )
    ^
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\ingest.py", line 1740, in _embed_chunks
    embeddings = gemini.embed_texts(
        texts
    )
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\gemini.py", line 108, inembed_texts
    self._embed_batch(
    ~~~~~~~~~~~~~~~~~^
        batch
        ^^^^^
    )
    ^
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\gemini.py", line 225, in_embed_batch
    raise EmbeddingError(
        "The embedding request failed."
    ) from exc
app.errors.EmbeddingError: The embedding request failed.
INFO:     127.0.0.1:60910 - "POST /documents/upload HTTP/1.1" 502 Bad Gateway
2026-09-18 19:27:20,093 ERROR app.api - document_list_failed
Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\api.py", line 138, in list_documents
    _document_response(
    ~~~~~~~~~~~~~~~~~~^
        document
        ^^^^^^^^
    )
    ^
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\api.py", line 452, in _document_response
    return DocumentResponse(
        id=str(
    ...<32 lines>...
        ),
    )
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\pydantic\main.py", line 263, in __init__
    validated_self = self.__pydantic_validator__.validate_python(data, self_instance=self)
pydantic_core._pydantic_core.ValidationError: 1 validation error for DocumentResponse
document_id
  Field required [type=missing, input_value={'id': 'b4c770ec-0254-40a...T13:57:20.014296+00:00'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
INFO:     127.0.0.1:60911 - "GET /documents HTTP/1.1" 500 Internal Server Error
INFO:     Shutting down
INFO:     Waiting for application shutdown.
2026-09-18 19:40:44,210 INFO app.main - application_stopped
INFO:     Application shutdown complete.
INFO:     Finished server process [19472]
INFO:     Stopping reloader process [19092]
