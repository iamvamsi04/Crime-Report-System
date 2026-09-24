max_retries = 5
    attempt = 0        

    responses = []

    for index, batch in enumerate(batches):
        try:
            batches = chunk_text(
        prompt,
        max_chars=6000,
    )
            batch_prompt = f"""
        You are processing part {index + 1}
        of {len(batches)} of a larger request.

        Analyze the following information carefully.
        Do not invent information.

        CONTENT:
        {batch}
        """

            response = _client.models.generate_content(
                model=LLM_MODEL,
                contents=batch_prompt,
                config=types.GenerateContentConfig(
                    temperature=LLM_TEMPERATURE,
                    response_mime_type="application/json",
                ),
            )

            responses.append(
                response.text
            )

            if index < len(batches) - 1:
                time.sleep(2)
            

        except Exception as exc:
            log.warning(
            "Gemini generation failed on attempt %d/%d: %s",
            attempt + 1,
            max_retries,
            exc,
        )

        if attempt == max_retries - 1:
            log.exception(
                "Gemini generation failed after all retries."
            )
            raise RuntimeError(
                "The language model could not process the request."
            ) from exc

        delay = 2 ** attempt

        log.info(
            "Retrying Gemini request in %d seconds.",
            delay,
        )

        time.sleep(delay)
