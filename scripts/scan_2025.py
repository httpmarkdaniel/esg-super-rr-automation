from __future__ import annotations

import io
import sys
import tempfile
import threading
import time

from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)

from pathlib import Path
from typing import Callable, TypeVar

from googleapiclient.http import (
    MediaIoBaseDownload,
)

from app.consolidation import (
    consolidate_records,
)

from app.google_auth import (
    get_drive_service,
)

from app.google_sheets import (
    get_existing_rr_descriptions,
    make_row_key,
    write_rows_bulk,
)

from app.metadata import (
    extract_account_metadata,
)


ROOT = Path(
    __file__
).resolve().parents[1]

ENGINE_DIR = ROOT / "engine"

if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(
        0,
        str(ENGINE_DIR),
    )

from rr_pdf_consolidator.extract import (
    inspect_rr,
)


# ============================================================
# CONFIG
# ============================================================

ROOT_FOLDER_ID = (
    "1mImklv-_IvxiKT6yrCvyz5le4T6eCZk8"
)

# Historical batch size.
#
# Keep 25 while validating this optimized version.
# After validation:
#
# TEST_LIMIT = None
#
TEST_LIMIT: int | None = 2


# Number of PDFs processed simultaneously.
#
# 4 is conservative.
# 6 is a good default for your workload.
#
MAX_WORKERS = 1


# Google retry configuration.

GOOGLE_RETRY_ATTEMPTS = 5

RETRY_DELAYS = [
    2,
    4,
    8,
    12,
    20,
]


FOLDER_MIME_TYPE = (
    "application/vnd.google-apps.folder"
)

PDF_MIME_TYPE = (
    "application/pdf"
)


T = TypeVar("T")


# Each worker thread gets its OWN Drive service.
#
# googleapiclient HTTP objects should not be
# shared between threads.

THREAD_LOCAL = threading.local()


# ============================================================
# RETRIES
# ============================================================

def with_retry(
    operation_name: str,
    func: Callable[[], T],
) -> T:
    last_error: Exception | None = None

    for attempt in range(
        1,
        GOOGLE_RETRY_ATTEMPTS + 1,
    ):
        try:
            return func()

        except Exception as exc:
            last_error = exc

            if (
                attempt
                >= GOOGLE_RETRY_ATTEMPTS
            ):
                break

            delay = RETRY_DELAYS[
                min(
                    attempt - 1,
                    len(RETRY_DELAYS) - 1,
                )
            ]

            print(
                f"    {operation_name} error "
                f"({attempt}/"
                f"{GOOGLE_RETRY_ATTEMPTS}): "
                f"{type(exc).__name__}"
            )

            print(
                f"    retry in {delay}s"
            )

            time.sleep(delay)

    assert last_error is not None

    raise last_error


# ============================================================
# DRIVE SERVICE PER THREAD
# ============================================================

def worker_drive_service():
    service = getattr(
        THREAD_LOCAL,
        "drive_service",
        None,
    )

    if service is None:
        service = get_drive_service()

        THREAD_LOCAL.drive_service = (
            service
        )

    return service


# ============================================================
# DRIVE SCAN
# ============================================================

def list_children(
    service,
    folder_id: str,
) -> list[dict]:
    items = []

    page_token = None

    while True:

        def request():
            return (
                service.files()
                .list(
                    q=(
                        f"'{folder_id}' "
                        "in parents and "
                        "trashed = false"
                    ),
                    spaces="drive",
                    fields=(
                        "nextPageToken,"
                        "files("
                        "id,"
                        "name,"
                        "mimeType,"
                        "parents"
                        ")"
                    ),
                    pageToken=page_token,
                    pageSize=1000,
                )
                .execute()
            )

        response = with_retry(
            "Drive listing",
            request,
        )

        items.extend(
            response.get(
                "files",
                [],
            )
        )

        page_token = response.get(
            "nextPageToken"
        )

        if not page_token:
            break

    return items


def is_summary_pdf(
    name: str,
) -> bool:
    return (
        "summary of receiving report"
        in name.casefold()
    )


def is_processed(
    name: str,
) -> bool:
    upper = name.upper()

    return (
        "(DONE)" in upper
        or "(REVIEW)" in upper
    )


def scan_folder(
    service,
    folder_id: str,
    path: str = "",
) -> list[dict]:
    """
    Recursive Drive scan.

    Does NOT print all 1600 filenames.
    """

    pdfs = []

    children = list_children(
        service,
        folder_id,
    )

    for item in children:

        name = item["name"]

        current_path = (
            f"{path}/{name}"
            if path
            else name
        )

        if (
            item["mimeType"]
            == FOLDER_MIME_TYPE
        ):
            pdfs.extend(
                scan_folder(
                    service,
                    item["id"],
                    current_path,
                )
            )

            continue

        if (
            item["mimeType"]
            != PDF_MIME_TYPE
        ):
            continue

        if is_summary_pdf(name):
            continue

        pdfs.append(
            {
                "id": item["id"],
                "name": name,
                "path": current_path,
                "processed": is_processed(
                    name
                ),
            }
        )

    return pdfs


# ============================================================
# PDF DOWNLOAD
# ============================================================

def download_pdf(
    file_id: str,
) -> Path:

    service = worker_drive_service()

    def perform_download():
        request = (
            service.files()
            .get_media(
                fileId=file_id
            )
        )

        buffer = io.BytesIO()

        downloader = (
            MediaIoBaseDownload(
                buffer,
                request,
            )
        )

        finished = False

        while not finished:
            (
                _,
                finished,
            ) = (
                downloader
                .next_chunk()
            )

        buffer.seek(0)

        return buffer

    buffer = with_retry(
        "Drive download",
        perform_download,
    )

    with tempfile.NamedTemporaryFile(
        suffix=".pdf",
        delete=False,
    ) as tmp:

        tmp.write(
            buffer.read()
        )

        return Path(
            tmp.name
        )


# ============================================================
# BUSINESS ROW MAPPING
# ============================================================

def build_business_rows(
    consolidated: list[dict],
    metadata: dict[str, str],
) -> list[dict]:

    output = []

    for item in consolidated:

        output.append(
            {
                "rr_number": str(
                    item.get(
                        "rr_reference_no"
                    )
                    or ""
                ).strip(),

                "account_name": str(
                    metadata.get(
                        "account_name"
                    )
                    or ""
                ).strip(),

                "billing_address": str(
                    metadata.get(
                        "billing_address"
                    )
                    or ""
                ).strip(),

                "company_name": str(
                    item.get(
                        "rr_company_name"
                    )
                    or ""
                ).strip(),

                "received_date": str(
                    item.get(
                        "rr_received_date"
                    )
                    or ""
                ).strip(),

                "description": str(
                    item.get(
                        "description"
                    )
                    or ""
                ).strip(),

                "net_weight": str(
                    item.get(
                        "net_weight"
                    )
                    or ""
                ).strip(),

                "category": str(
                    item.get(
                        "category"
                    )
                    or ""
                ).strip(),

                "order_type": str(
                    item.get(
                        "rr_order_type"
                    )
                    or ""
                ).strip(),

                "remarks": str(
                    item.get(
                        "remarks"
                    )
                    or ""
                ).strip(),

                "receiving_notes": str(
                    item.get(
                        "rr_receiving_notes"
                    )
                    or ""
                ).strip(),
            }
        )

    return output


# ============================================================
# PARALLEL PDF EXTRACTION
# ============================================================

def extract_one_pdf(
    pdf: dict,
) -> dict:
    """
    Worker function.

    Downloads + extracts ONE PDF.

    Does NOT touch Google Sheets.
    Does NOT rename Drive files.
    """

    temp_path = None

    try:

        temp_path = download_pdf(
            pdf["id"]
        )

        result = inspect_rr(
            temp_path
        )

        if (
            result.status
            != "RR_EXTRACTION_SUCCEEDED"
        ):
            return {
                "pdf": pdf,
                "status": "REVIEW",
                "reason": (
                    f"Extraction status: "
                    f"{result.status}"
                ),
                "rows": [],
            }

        metadata = (
            extract_account_metadata(
                temp_path
            )
        )

        raw_records = [
            vars(record)
            for record
            in result.description_records
        ]

        consolidated = (
            consolidate_records(
                raw_records
            )
        )

        rows = build_business_rows(
            consolidated,
            metadata,
        )

        if not rows:
            return {
                "pdf": pdf,
                "status": "REVIEW",
                "reason": (
                    "No usable RR rows"
                ),
                "rows": [],
            }

        return {
            "pdf": pdf,
            "status": "SUCCESS",
            "rr_number": (
                result.header_values.get(
                    "reference_no"
                )
            ),
            "raw_count": len(
                raw_records
            ),
            "unique_count": len(
                rows
            ),
            "rows": rows,
        }

    except Exception as exc:

        return {
            "pdf": pdf,
            "status": "FAILED",
            "reason": (
                f"{type(exc).__name__}: "
                f"{exc}"
            ),
            "rows": [],
        }

    finally:

        if temp_path:
            temp_path.unlink(
                missing_ok=True
            )


# ============================================================
# DRIVE RENAME
# ============================================================

def done_filename(
    filename: str,
) -> str:

    if "(DONE)" in filename.upper():
        return filename

    if filename.lower().endswith(
        ".pdf"
    ):
        return (
            filename[:-4]
            + " (DONE).pdf"
        )

    return (
        filename
        + " (DONE)"
    )


def rename_done(
    pdf: dict,
) -> None:

    service = worker_drive_service()

    new_name = done_filename(
        pdf["name"]
    )

    with_retry(
        "Drive rename",
        lambda: (
            service.files()
            .update(
                fileId=pdf["id"],
                body={
                    "name": new_name,
                },
                fields="id,name",
            )
            .execute()
        ),
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    started = time.perf_counter()

    print()
    print(
        "ESG RR 2025 "
        "HIGH-SPEED AUTOMATION"
    )
    print(
        "=" * 60
    )

    #
    # STEP 1
    # Scan Drive ONCE
    #

    drive = get_drive_service()

    print(
        "Scanning Google Drive..."
    )

    all_pdfs = scan_folder(
        drive,
        ROOT_FOLDER_ID,
    )

    pending = [
        pdf
        for pdf in all_pdfs
        if not pdf[
            "processed"
        ]
    ]

    already_done = (
        len(all_pdfs)
        - len(pending)
    )

    print(
        f"RR PDFs found : "
        f"{len(all_pdfs)}"
    )

    print(
        f"Already done  : "
        f"{already_done}"
    )

    print(
        f"Pending       : "
        f"{len(pending)}"
    )

    #
    # TEST LIMIT
    #

    if TEST_LIMIT is not None:
        batch = pending[
            :TEST_LIMIT
        ]
    else:
        batch = pending

    print(
        f"Processing    : "
        f"{len(batch)}"
    )

    print(
        f"Workers       : "
        f"{MAX_WORKERS}"
    )

    print(
        "=" * 60
    )

    #
    # STEP 2
    # Parallel extraction
    #

    results = []

    completed = 0

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                extract_one_pdf,
                pdf,
            ): pdf
            for pdf in batch
        }

        for future in as_completed(
            futures
        ):

            result = future.result()

            results.append(
                result
            )

            completed += 1

            pdf = result["pdf"]

            if (
                result["status"]
                == "SUCCESS"
            ):
                print(
                    f"[{completed}/"
                    f"{len(batch)}] "
                    f"OK "
                    f"{pdf['name']} "
                    f"→ "
                    f"{result['unique_count']} "
                    "items"
                )

            else:
                print(
                    f"[{completed}/"
                    f"{len(batch)}] "
                    f"{result['status']} "
                    f"{pdf['name']}"
                )

    #
    # STEP 3
    # Load Sheet duplicate keys ONCE
    #

    print()
    print(
        "Loading existing MAIN "
        "records..."
    )

    existing = with_retry(
        "Sheets duplicate read",
        get_existing_rr_descriptions,
    )

    print(
        f"Existing RR items: "
        f"{len(existing)}"
    )

    #
    # STEP 4
    # Build one bulk write
    #

    rows_to_write = []

    seen_new = set()

    successful_files = []

    failed_results = []

    for result in results:

        if (
            result["status"]
            != "SUCCESS"
        ):
            failed_results.append(
                result
            )

            continue

        successful_files.append(
            result["pdf"]
        )

        for row in result["rows"]:

            key = make_row_key(
                row
            )

            if not all(key):
                continue

            if key in existing:
                continue

            if key in seen_new:
                continue

            seen_new.add(
                key
            )

            rows_to_write.append(
                row
            )

    print()
    print(
        f"New Sheet rows: "
        f"{len(rows_to_write)}"
    )

    #
    # STEP 5
    # ONE BULK SHEET WRITE
    #

    if rows_to_write:

        print(
            "Writing batch to MAIN..."
        )

        written = with_retry(
            "Sheets bulk write",
            lambda: write_rows_bulk(
                rows_to_write
            ),
        )

        print(
            f"Rows written: {written}"
        )

    else:

        print(
            "No new Sheet rows needed."
        )

    #
    # STEP 6
    # Rename successful PDFs
    #
    # Only happens AFTER Sheets write
    # completes successfully.
    #

    print()
    print(
        "Marking successful PDFs DONE..."
    )

    renamed = 0

    rename_failures = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        rename_futures = {
            executor.submit(
                rename_done,
                pdf,
            ): pdf
            for pdf
            in successful_files
        }

        for future in as_completed(
            rename_futures
        ):

            pdf = rename_futures[
                future
            ]

            try:

                future.result()

                renamed += 1

            except Exception as exc:

                rename_failures.append(
                    (
                        pdf,
                        str(exc),
                    )
                )

    #
    # STEP 7
    # SUMMARY
    #

    elapsed = (
        time.perf_counter()
        - started
    )

    print()
    print(
        "=" * 60
    )

    print(
        "BATCH COMPLETE"
    )

    print()

    print(
        f"PDFs attempted    : "
        f"{len(batch)}"
    )

    print(
        f"Extracted OK      : "
        f"{len(successful_files)}"
    )

    print(
        f"Sheet rows written: "
        f"{len(rows_to_write)}"
    )

    print(
        f"Files marked DONE : "
        f"{renamed}"
    )

    print(
        f"Extraction failures: "
        f"{len(failed_results)}"
    )

    print(
        f"Rename failures   : "
        f"{len(rename_failures)}"
    )

    print(
        f"Elapsed time      : "
        f"{elapsed:.1f} seconds"
    )

    if batch:

        print(
            f"Average per PDF   : "
            f"{elapsed / len(batch):.2f} "
            "seconds"
        )

    if failed_results:

        print()
        print(
            "REVIEW / FAILED:"
        )

        for result in failed_results:

            print(
                " -",
                result[
                    "pdf"
                ][
                    "path"
                ],
            )

            print(
                "   ",
                result.get(
                    "reason",
                    "",
                ),
            )

    if rename_failures:

        print()
        print(
            "RENAME FAILURES:"
        )

        for pdf, error in (
            rename_failures
        ):

            print(
                f" - {pdf['path']}"
            )

            print(
                f"   {error}"
            )

    print()
    print(
        "=" * 60
    )


if __name__ == "__main__":
    main()