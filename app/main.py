from __future__ import annotations

import csv
import io
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"

if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from rr_pdf_consolidator.extract import inspect_rr  # noqa: E402
from app.consolidation import consolidate_records  # noqa: E402
from app.metadata import extract_account_metadata  # noqa: E402


app = FastAPI(
    title="ESG RR Consolidation Portal",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

LAST_RESULT: dict | None = None


OUTPUT_COLUMNS = (
    ("RR Number", "rr_number"),
    ("Account Name", "account_name"),
    ("Billing Address", "billing_address"),
    ("Company Name", "company_name"),
    ("Received Date", "received_date"),
    ("Description", "description"),
    ("Net Weight", "net_weight"),
    ("Category", "category"),
    ("Order Type", "order_type"),
    ("Remarks", "remarks"),
    ("Receiving Notes", "receiving_notes"),
)


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (
        ROOT / "static" / "index.html"
    ).read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "esg-rr-portal",
        "version": "0.2.0",
    }


def _business_row(
    item: dict,
    metadata: dict[str, str],
) -> dict[str, str]:
    return {
        "rr_number": str(
            item.get("rr_reference_no") or ""
        ).strip(),

        "account_name": metadata.get(
            "account_name",
            "",
        ),

        "billing_address": metadata.get(
            "billing_address",
            "",
        ),

        "company_name": str(
            item.get("rr_company_name") or ""
        ).strip(),

        "received_date": str(
            item.get("rr_received_date") or ""
        ).strip(),

        "description": str(
            item.get("description") or ""
        ).strip(),

        "net_weight": str(
            item.get("net_weight") or ""
        ).strip(),

        "category": str(
            item.get("category") or ""
        ).strip(),

        "order_type": str(
            item.get("rr_order_type") or ""
        ).strip(),

        "remarks": str(
            item.get("remarks") or ""
        ).strip(),

        "receiving_notes": str(
            item.get("rr_receiving_notes") or ""
        ).strip(),
    }


@app.post("/api/rr/parse")
async def parse_rr(
    file: UploadFile = File(...),
) -> dict:
    global LAST_RESULT

    if (
        not file.filename
        or not file.filename.lower().endswith(".pdf")
    ):
        raise HTTPException(
            400,
            "Please upload a PDF file.",
        )

    payload = await file.read()

    if not payload:
        raise HTTPException(
            400,
            "The uploaded PDF is empty.",
        )

    with tempfile.NamedTemporaryFile(
        suffix=".pdf",
        delete=False,
    ) as tmp:
        tmp.write(payload)
        temp_path = Path(tmp.name)

    try:
        result = inspect_rr(temp_path)

        supplemental = extract_account_metadata(
            temp_path
        )

    finally:
        temp_path.unlink(missing_ok=True)

    if result.status != "RR_EXTRACTION_SUCCEEDED":
        raise HTTPException(
            422,
            {
                "status": result.status,
                "errors": result.errors,
                "warnings": result.warnings,
            },
        )

    raw_records = [
        vars(record)
        for record in result.description_records
    ]

    consolidated_internal = consolidate_records(
        raw_records
    )

    business_rows = [
        _business_row(
            item,
            supplemental,
        )
        for item in consolidated_internal
    ]

    LAST_RESULT = {
        "filename": file.filename,
        "rr_number": result.header_values.get(
            "reference_no"
        ),
        "raw_line_item_count": len(raw_records),
        "consolidated_item_count": len(
            business_rows
        ),
        "duplicates_collapsed": (
            len(raw_records)
            - len(business_rows)
        ),
        "items": business_rows,
        "warnings": result.warnings,
    }

    return LAST_RESULT


@app.get("/api/rr/export.csv")
def export_csv() -> StreamingResponse:
    if not LAST_RESULT:
        raise HTTPException(
            404,
            "No parsed RR is available to export yet.",
        )

    buffer = io.StringIO()

    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            label
            for label, _ in OUTPUT_COLUMNS
        ],
    )

    writer.writeheader()

    for item in LAST_RESULT["items"]:
        writer.writerow(
            {
                label: item.get(key, "")
                for label, key in OUTPUT_COLUMNS
            }
        )

    data = buffer.getvalue().encode(
        "utf-8-sig"
    )

    return StreamingResponse(
        iter([data]),
        media_type="text/csv",
        headers={
            "Content-Disposition":
            "attachment; filename=rr-consolidated.csv"
        },
    )