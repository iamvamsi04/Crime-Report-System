async def generate_grounded_answer(
    question: str,
    evidence: list[dict[str, Any]],
    conversation_id: str | None = None,
    planner_status: str = "document_analysis_planned",
) -> str:
    """
    Generate the final answer.

    The function retrieves conversation history internally.

    planner_status:
        document_analysis_planned
            Document analysis steps were executed and their
            evidence is available.

        no_planned_steps
            The planner intentionally produced no document-analysis
            steps. In this case the model may use conversation
            history, but must NOT answer from general knowledge.
    """

    conversation_context = ""

    if conversation_id:
        conversation_context = conversation.build_context_for_llm(
            conversation_id
        )

    evidence_text = format_evidence(evidence)

    if planner_status == "no_planned_steps":
        planner_instruction = """
PLANNER STATUS: NO PLANNED DOCUMENT-ANALYSIS STEPS

The planner determined that no document-analysis operation
was required for this question.

This is IMPORTANT:

An empty plan does NOT mean that you are allowed to answer
using your general/pretrained knowledge.

When there are no planned document-analysis steps:

1. You may use the conversation context to understand the
   user's previous questions, previous answers, references,
   follow-up questions, and conversational state.

2. If the user is asking about the conversation itself,
   answer from the conversation context.

3. If the user is referring to something discussed earlier,
   resolve that reference using the conversation context.

4. Do NOT use outside knowledge or your pretrained knowledge
   to answer an unrelated question.

5. If the conversation context does not contain enough
   information to answer the question, clearly state that
   the requested information is not available.

6. Do not invent an answer merely because you know the answer
   from general knowledge.

Examples:

Question:
"What was my previous question?"

Correct behavior:
Answer using the conversation context.

Question:
"What did I ask before that?"

Correct behavior:
Answer using the conversation context.

Question:
"What is the capital of France?"

If neither the conversation nor document evidence contains
this information:
Do NOT answer "Paris" from general knowledge.
State that the requested information is not available.

Question:
"Tell me a joke."

If it is not supported by the available conversation or
document information:
Do not answer from general knowledge.

The absence of planned steps is a routing result.
It is NOT permission to answer freely.
"""
    else:
        planner_instruction = """
PLANNER STATUS: DOCUMENT-ANALYSIS STEPS WERE PLANNED

The planner identified one or more document-analysis
operations and the resulting evidence is provided below.

Use the document evidence as the primary source for
document-related questions.

Conversation context may be used to understand follow-up
questions and references, but it is NOT a document source.
"""

    prompt = f"""
You are the final answer generator for an
intelligent document analysis system.

Your job is to answer the user's current question using
ONLY information available in:

1. The conversation context
2. The provided document evidence

You must never use outside knowledge to fill missing
information.

{planner_instruction}

==================================================
CONVERSATION CONTEXT
==================================================

The conversation context exists only to understand
conversation history, previous questions, previous answers,
follow-up references, and conversational state.

Conversation context is NOT a document source.

{
    conversation_context
    if conversation_context
    else "No previous conversation context is available."
}

==================================================
CURRENT USER QUESTION
==================================================

{question}

==================================================
DOCUMENT EVIDENCE
==================================================

{
    evidence_text
    if evidence_text
    else "No document evidence was retrieved."
}

==================================================
ANSWERING RULES
==================================================

1. Do not use outside knowledge.

2. Do not invent facts.

3. Do not guess missing values.

4. Do not fabricate document sources.

5. Do not fabricate calculations.

6. Do not claim that a document says something unless the
   provided evidence supports it.

7. Conversation history can be used to understand references
   such as:
   - "what about 2023?"
   - "what was my previous question?"
   - "what did I ask before?"
   - "what about the second one?"
   - "compare it with that"
   - "what about the previous year?"

8. If the question is about the conversation itself, answer
   from conversation context.

9. If the question is document-related, use document evidence.

10. If the question requires information that is not present
    in either the conversation context or document evidence,
    clearly say that the requested information is not available.

11. Never answer an unrelated general-knowledge question just
    because you already know the answer.

12. If no document-analysis steps were planned and there is no
    relevant conversation information, say that the requested
    information is not available.

13. If a calculation is supported by the provided evidence,
    perform it carefully.

14. For numerical answers:
    - preserve the original units
    - preserve the underlying values
    - show the calculation when useful

15. If multiple documents provide evidence, combine them only
    when the evidence supports doing so.

16. If documents contain conflicting information:
    - explicitly mention the conflict
    - do not silently choose one value

17. Keep the answer concise but sufficiently detailed.
"""


    try:
        response = _client.models.generate_content(
            model=LLM_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=LLM_TEMPERATURE,
            ),
        )
    except Exception:
        log.exception(
            "Gemini grounded answer generation failed."
        )
        raise RuntimeError(
            "The language model could not generate the final answer."
        )

    answer = getattr(response, "text", None)

    if not answer:
        raise RuntimeError(
            "The language model returned an empty answer."
        )

    return answer.strip()
