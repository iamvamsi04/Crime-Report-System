
def format_evidence(
    evidence: list[dict[str, Any]],
) -> str:
    """
    Convert retrieved document evidence into text that can be
    supplied to the final LLM.
    """

    if not evidence:
        return ""

    sections: list[str] = []

    for index, item in enumerate(evidence, start=1):
        source_type = item.get(
            "source_type",
            "unknown",
        )

        filename = item.get(
            "filename",
            "Unknown document",
        )

        document_id = item.get(
            "document_id",
            "Unknown document ID",
        )

        content = item.get(
            "content",
            "",
        )

        metadata = item.get(
            "metadata",
            {},
        )

        section_lines = [
            f"Evidence {index}",
            f"Source type: {source_type}",
            f"Document ID: {document_id}",
            f"Filename: {filename}",
        ]

        page = metadata.get("page")

        if page is not None:
            section_lines.append(
                f"Page: {page}"
            )

        section = metadata.get("section")

        if section:
            section_lines.append(
                f"Section: {section}"
            )

        section_lines.extend(
            [
                "Content:",
                str(content),
            ]
        )

        sections.append(
            "\n".join(section_lines)
        )

    return "\n\n--------------------\n\n".join(
        sections
    )
