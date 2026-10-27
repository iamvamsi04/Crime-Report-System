 billing_turn mode=admin total_seconds=29.295 lock_wait_seconds=0.000 tool_calls=0 tool_seconds=0.000 charts=0 model_calls=0 input_tokens=0 output_tokens=0
Billing turn failed: 9b565288-280b-4f6d-b0cc-c6b78c17440d
Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpx2\_transports\default.py", line 98, in map_httpcore_exceptions
    yield
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpx2\_transports\default.py", line 388, in handle_async_request
    resp = await self._pool.handle_async_request(req)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpcore2\_async\connection_pool.py", line 242, in handle_async_request
    raise exc from None
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpcore2\_async\connection_pool.py", line 224, in handle_async_request
    response = await connection.handle_async_request(pool_request.request)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpcore2\_async\connection.py", line 96, in handle_async_request
    return await self._connection.handle_async_request(request)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpcore2\_async\http11.py", line 125, in handle_async_request
    raise exc
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpcore2\_async\http11.py", line 97, in handle_async_request
    ) = await self._receive_response_headers(**kwargs)
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpcore2\_async\http11.py", line 167, in _receive_response_headers
    event = await self._receive_event(timeout=timeout)
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpcore2\_async\http11.py", line 212, in _receive_event
    raise RemoteProtocolError(msg)
httpcore2.RemoteProtocolError: Server disconnected without sending a response.

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\openai\_base_client.py", line 1773, in request
    response = await self._send_request(
               ^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<3 lines>...
    )
    ^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\openai\_client.py", line 1361, in _send_request
    response = await self._send_with_auth_retry(request, stream=stream, **kwargs)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\openai\_client.py", line 1340, in _send_with_auth_retry
    response = await super()._send_request(request, stream=stream, **kwargs)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\openai\_base_client.py", line 1690, in _send_request
    return await self._client.send(request, stream=stream, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpx2\_client.py", line 1818, in send
    response = await self._send_handling_auth(
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<4 lines>...
    )
    ^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpx2\_client.py", line 1846, in _send_handling_auth
    response = await self._send_handling_redirects(
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<3 lines>...
    )
    ^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpx2\_client.py", line 1881, in _send_handling_redirects
    response = await self._send_single_request(request)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpx2\_client.py", line 1915, in _send_single_request
    response = await transport.handle_async_request(request)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpx2\_transports\default.py", line 387, in handle_async_request
    with map_httpcore_exceptions():
         ~~~~~~~~~~~~~~~~~~~~~~~^^
  File "C:\Users\T00934\AppData\Local\Programs\Python\Python314\Lib\contextlib.py", line 162, in __exit__
    self.gen.throw(value)
    ~~~~~~~~~~~~~~^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\httpx2\_transports\default.py", line 115, in map_httpcore_exceptions
    raise mapped_exc(message) from exc
httpx2.RemoteProtocolError: Server disconnected without sending a response.

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\billing_agent\api\main.py", line 328, in run_chat
    result = await asyncio.wait_for(app.state.engine.chat(body.message, context.history(cid), record["customer_id"], **kwargs),
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                    timeout=settings.CHAT_TIMEOUT)
                                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\AppData\Local\Programs\Python\Python314\Lib\asyncio\tasks.py", line 488, in wait_for
    return await fut
           ^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\agent\billing_agent.py", line 897, in chat
    result = await agent.run(task=message,cancellation_token=run_token)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\autogen_agentchat\agents\_base_chat_agent.py", line 149, in run
    response = await self.on_messages(input_messages, cancellation_token)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\autogen_agentchat\agents\_assistant_agent.py", line 896, in on_messages
    async for message in self.on_messages_stream(messages, cancellation_token):
        if isinstance(message, Response):
            return message
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\autogen_agentchat\agents\_assistant_agent.py", line 953, in on_messages_stream
    async for inference_output in self._call_llm(
    ...<15 lines>...
            yield inference_output
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\autogen_agentchat\agents\_assistant_agent.py", line 1109, in _call_llm
    model_result = await model_client.create(
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<4 lines>...
    )
    ^
  File "C:\Users\T00934\Desktop\billing_agent\agent\billing_agent.py", line 96, in create
    result = await super().create(*args, **kwargs)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\autogen_ext\models\openai\_openai_client.py", line 704, in create
    result: Union[ParsedChatCompletion[BaseModel], ChatCompletion] = await future
                                                                     ^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\openai\resources\chat\completions\completions.py", line 2952, in create
    return await self._post(
           ^^^^^^^^^^^^^^^^^
    ...<55 lines>...
    )
    ^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\openai\_base_client.py", line 2053, in post
    return await self.request(cast_to, opts, stream=stream, stream_cls=stream_cls)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\billing_agent\.venv\Lib\site-packages\openai\_base_client.py", line 1808, in request
    raise APIConnectionError(request=request) from err
openai.APIConnectionError: Connection error.
INFO:     billing_stream event=error seconds=29.708
