# Crime-Report-System


2026-09-18 20:10:05,636 ERROR app.gemini - gemini_embedding_failed
Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_transports\default.py", line 101, in map_httpcore_exceptions
    yield
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_transports\default.py", line 250, in handle_request
    resp = self._pool.handle_request(req)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_sync\connection_pool.py", line 256, in handle_request
    raise exc from None
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_sync\connection_pool.py", line 236, in handle_request
    response = connection.handle_request(
        pool_request.request
    )
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_sync\connection.py", line 101, in handle_request
    raise exc
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_sync\connection.py", line 78, in handle_request
    stream = self._connect(request)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_sync\connection.py", line 124, in _connect
    stream = self._network_backend.connect_tcp(**kwargs)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_backends\sync.py", line 207, in connect_tcp
    with map_exceptions(exc_map):
         ~~~~~~~~~~~~~~^^^^^^^^^
  File "C:\Users\T00934\AppData\Local\Programs\Python\Python314\Lib\contextlib.py", line 162, in __exit__
    self.gen.throw(value)
    ~~~~~~~~~~~~~~^^^^^^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_exceptions.py", line 14, in map_exceptions
    raise to_exc(exc) from exc
httpcore.ConnectError: [Errno 11001] getaddrinfo failed

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\gemini.py", line 207, in _embed_batch
    self.client.models.embed_content(
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^
        model=(
        ^^^^^^^
    ...<7 lines>...
        },
        ^^
    )
    ^
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\google\genai\models.py", line 6125, in embed_content
    return self._embed_content(model=model, contents=contents, config=config)
           ~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\google\genai\models.py", line 4936, in _embed_content
    response = self._api_client.request(
        'post', path, request_dict, http_options
    )
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\google\genai\_api_client.py", line 1750, in request
    response = self._request(http_request, http_options, stream=False)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\google\genai\_api_client.py", line 1537, in _request
    return self._retry(self._request_once, http_request, stream)  # type: ignore[no-any-return]
           ~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\tenacity\__init__.py", line 470, in __call__
    do = self.iter(retry_state=retry_state)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\tenacity\__init__.py", line 371, in iter
    result = action(retry_state)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\tenacity\__init__.py", line 413, in exc_check
    raise retry_exc.reraise()
          ~~~~~~~~~~~~~~~~~^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\tenacity\__init__.py", line 184, in reraise
    raise self.last_attempt.result()
          ~~~~~~~~~~~~~~~~~~~~~~~~^^
  File "C:\Users\T00934\AppData\Local\Programs\Python\Python314\Lib\concurrent\futures\_base.py", line 443, in result
    return self.__get_result()
           ~~~~~~~~~~~~~~~~~^^
  File "C:\Users\T00934\AppData\Local\Programs\Python\Python314\Lib\concurrent\futures\_base.py", line 395, in __get_result
    raise self._exception
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\tenacity\__init__.py", line 473, in __call__
    result = fn(*args, **kwargs)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\google\genai\_api_client.py", line 1513, in _request_once
    response = self._httpx_client.send(httpx_request, stream=stream)  # type: ignore[union-attr, arg-type]
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_client.py", line 914, in send
    response = self._send_handling_auth(
        request,
    ...<2 lines>...
        history=[],
    )
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_client.py", line 942, in _send_handling_auth
    response = self._send_handling_redirects(
        request,
        follow_redirects=follow_redirects,
        history=history,
    )
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_client.py", line 979, in _send_handling_redirects
    response = self._send_single_request(request)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_client.py", line 1014, in _send_single_request
    response = transport.handle_request(request)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_transports\default.py", line 249, in handle_request
    with map_httpcore_exceptions():
         ~~~~~~~~~~~~~~~~~~~~~~~^^
  File "C:\Users\T00934\AppData\Local\Programs\Python\Python314\Lib\contextlib.py", line 162, in __exit__
    self.gen.throw(value)
    ~~~~~~~~~~~~~~^^^^^^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_transports\default.py", line 118, in map_httpcore_exceptions
    raise mapped_exc(message) from exc
httpx.ConnectError: [Errno 11001] getaddrinfo failed
2026-09-18 20:10:05,679 ERROR app.ingest - document_ingestion_failed document_id=8b98bb02-e25d-4eb6-b658-46ed754984d0 filename=2014_economy.txt.txt
Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_transports\default.py", line 101, in map_httpcore_exceptions
    yield
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_transports\default.py", line 250, in handle_request
    resp = self._pool.handle_request(req)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_sync\connection_pool.py", line 256, in handle_request
    raise exc from None
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_sync\connection_pool.py", line 236, in handle_request
    response = connection.handle_request(
        pool_request.request
    )
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_sync\connection.py", line 101, in handle_request
    raise exc
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_sync\connection.py", line 78, in handle_request
    stream = self._connect(request)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_sync\connection.py", line 124, in _connect
    stream = self._network_backend.connect_tcp(**kwargs)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_backends\sync.py", line 207, in connect_tcp
    with map_exceptions(exc_map):
         ~~~~~~~~~~~~~~^^^^^^^^^
  File "C:\Users\T00934\AppData\Local\Programs\Python\Python314\Lib\contextlib.py", line 162, in __exit__
    self.gen.throw(value)
    ~~~~~~~~~~~~~~^^^^^^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpcore\_exceptions.py", line 14, in map_exceptions
    raise to_exc(exc) from exc
httpcore.ConnectError: [Errno 11001] getaddrinfo failed

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\gemini.py", line 207, in _embed_batch
    self.client.models.embed_content(
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^
        model=(
        ^^^^^^^
    ...<7 lines>...
        },
        ^^
    )
    ^
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\google\genai\models.py", line 6125, in embed_content
    return self._embed_content(model=model, contents=contents, config=config)
           ~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\google\genai\models.py", line 4936, in _embed_content
    response = self._api_client.request(
        'post', path, request_dict, http_options
    )
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\google\genai\_api_client.py", line 1750, in request
    response = self._request(http_request, http_options, stream=False)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\google\genai\_api_client.py", line 1537, in _request
    return self._retry(self._request_once, http_request, stream)  # type: ignore[no-any-return]
           ~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\tenacity\__init__.py", line 470, in __call__
    do = self.iter(retry_state=retry_state)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\tenacity\__init__.py", line 371, in iter
    result = action(retry_state)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\tenacity\__init__.py", line 413, in exc_check
    raise retry_exc.reraise()
          ~~~~~~~~~~~~~~~~~^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\tenacity\__init__.py", line 184, in reraise
    raise self.last_attempt.result()
          ~~~~~~~~~~~~~~~~~~~~~~~~^^
  File "C:\Users\T00934\AppData\Local\Programs\Python\Python314\Lib\concurrent\futures\_base.py", line 443, in result
    return self.__get_result()
           ~~~~~~~~~~~~~~~~~^^
  File "C:\Users\T00934\AppData\Local\Programs\Python\Python314\Lib\concurrent\futures\_base.py", line 395, in __get_result
    raise self._exception
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\tenacity\__init__.py", line 473, in __call__
    result = fn(*args, **kwargs)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\google\genai\_api_client.py", line 1513, in _request_once
    response = self._httpx_client.send(httpx_request, stream=stream)  # type: ignore[union-attr, arg-type]
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_client.py", line 914, in send
    response = self._send_handling_auth(
        request,
    ...<2 lines>...
        history=[],
    )
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_client.py", line 942, in _send_handling_auth
    response = self._send_handling_redirects(
        request,
        follow_redirects=follow_redirects,
        history=history,
    )
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_client.py", line 979, in _send_handling_redirects
    response = self._send_single_request(request)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_client.py", line 1014, in _send_single_request
    response = transport.handle_request(request)
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_transports\default.py", line 249, in handle_request
    with map_httpcore_exceptions():
         ~~~~~~~~~~~~~~~~~~~~~~~^^
  File "C:\Users\T00934\AppData\Local\Programs\Python\Python314\Lib\contextlib.py", line 162, in __exit__
    self.gen.throw(value)
    ~~~~~~~~~~~~~~^^^^^^^
  File "C:\Users\T00934\Desktop\document_analysis_sys\.venv\Lib\site-packages\httpx\_transports\default.py", line 118, in map_httpcore_exceptions
    raise mapped_exc(message) from exc
httpx.ConnectError: [Errno 11001] getaddrinfo failed

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\ingest.py", line 165, in ingest_bytes
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
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\gemini.py", line 108, in embed_texts
    self._embed_batch(
    ~~~~~~~~~~~~~~~~~^
        batch
        ^^^^^
    )
    ^
  File "C:\Users\T00934\Desktop\document_analysis_sys\app\gemini.py", line 225, in _embed_batch
    raise EmbeddingError(
        "The embedding request failed."
    ) from exc
app.errors.EmbeddingError: The embedding request failed.
INFO:     127.0.0.1:54131 - "POST /documents/upload HTTP/1.1" 502 Bad Gateway
INFO:     127.0.0.1:62666 - "GET /documents HTTP/1.1" 200 OK
