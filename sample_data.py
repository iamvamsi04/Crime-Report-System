"""Generate small local PDF, TXT, and CSV samples for repeatable demonstrations."""

from __future__ import annotations

from pathlib import Path

import pymupdf


def create_sample_documents(target_directory: Path) -> list[Path]:
    """Create intentionally small, text-based documents with a known CSV conflict."""
    target_directory.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    pdf_path = target_directory / "annual_report_2024.pdf"
    pdf = pymupdf.open()
    page_one = pdf.new_page()
    page_one.insert_text(
        (72, 72),
        "ANNUAL REPORT 2024\n\nRevenue reached $320 million in 2024, compared with $280 million in 2023.\n"
        "The Sales department generated $140 million in 2024.\n\nMain risks: supplier concentration and foreign-exchange volatility.",
    )
    page_two = pdf.new_page()
    page_two.insert_text(
        (72, 72),
        "OPERATING REVIEW\n\nEngineering generated $150 million in 2024. Support generated $30 million.\n"
        "Management prioritised diversification of critical suppliers.",
    )
    pdf.save(pdf_path)
    pdf.close()
    created.append(pdf_path)

    text_path = target_directory / "risk_notes.txt"
    text_path.write_text(
        "RISK NOTES\n"
        "Supplier concentration remains a material operational risk.\n"
        "Foreign-exchange volatility may reduce margins.\n"
        "Cybersecurity incidents could disrupt customer support.\n",
        encoding="utf-8",
    )
    created.append(text_path)

    revenue_path = target_directory / "revenue_by_department.csv"
    revenue_path.write_text(
        "Year,Department,Revenue,Cost\n"
        "2023,Sales,120,80\n2024,Sales,140,95\n"
        "2023,Engineering,110,85\n2024,Engineering,150,105\n"
        "2023,Support,50,40\n2024,Support,30,28\n",
        encoding="utf-8",
    )
    created.append(revenue_path)

    conflict_path = target_directory / "finance_restatement.csv"
    conflict_path.write_text(
        "Year,Department,Revenue\n2024,Sales,145\n2024,Engineering,150\n2024,Support,30\n",
        encoding="utf-8",
    )
    created.append(conflict_path)
    return created


if __name__ == "__main__":
    from app.core.config import settings

    paths = create_sample_documents(settings.documents_dir)
    print("Created sample documents:")
    for path in paths:
        print(path)
