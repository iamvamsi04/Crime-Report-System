
Traceback (most recent call last):
  File "C:\Users\T00934\Desktop\b_agent\api\main.py", line 459, in run_chat
    result = await asyncio.wait_for(
             ^^^^^^^^^^^^^^^^^^^^^^^
        app.state.engine.chat(body.message, context.history(cid), record["customer_id"], **kwargs),
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
        timeout=settings.CHAT_TIMEOUT)
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\AppData\Local\Programs\Python\Python314\Lib\asyncio\tasks.py", line 488, in wait_for
    return await fut
           ^^^^^^^^^
  File "C:\Users\T00934\Desktop\b_agent\agent\billing_agent.py", line 1321, in chat
    result = await agent.run(task=message,cancellation_token=run_token)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\b_agent\.venv\Lib\site-packages\autogen_agentchat\agents\_base_chat_agent.py", line 149, in run
    response = await self.on_messages(input_messages, cancellation_token)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\b_agent\.venv\Lib\site-packages\autogen_agentchat\agents\_assistant_agent.py", line 896, in on_messages
    async for message in self.on_messages_stream(messages, cancellation_token):
        if isinstance(message, Response):
            return message
  File "C:\Users\T00934\Desktop\b_agent\.venv\Lib\site-packages\autogen_agentchat\agents\_assistant_agent.py", line 953, in on_messages_stream
    async for inference_output in self._call_llm(
    ...<15 lines>...
            yield inference_output
  File "C:\Users\T00934\Desktop\b_agent\.venv\Lib\site-packages\autogen_agentchat\agents\_assistant_agent.py", line 1109, in _call_llm
    model_result = await model_client.create(
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<4 lines>...
    )
    ^
  File "C:\Users\T00934\Desktop\b_agent\agent\billing_agent.py", line 121, in create
    result = await self._client.create(*args, **kwargs)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\b_agent\.venv\Lib\site-packages\autogen_ext\models\openai\_openai_client.py", line 704, in create
    result: Union[ParsedChatCompletion[BaseModel], ChatCompletion] = await future
                                                                     ^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\b_agent\.venv\Lib\site-packages\openai\resources\chat\completions\completions.py", line 2907, in create
    return await self._post(
           ^^^^^^^^^^^^^^^^^
    ...<55 lines>...
    )
    ^
  File "C:\Users\T00934\Desktop\b_agent\.venv\Lib\site-packages\openai\_base_client.py", line 1992, in post
    return await self.request(cast_to, opts, stream=stream, stream_cls=stream_cls)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\T00934\Desktop\b_agent\.venv\Lib\site-packages\openai\_base_client.py", line 1777, in request
    raise self._make_status_error_from_response(err.response) from None
openai.APIStatusError: Method Not Allowed
INFO:     127.0.0.1:49221 - "POST /chat HTTP/1.1" 503 Service Unavailable

