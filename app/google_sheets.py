from __future__ import annotations

from typing import Any

from app.google_auth import get_sheets_service


SPREADSHEET_ID = (
    "1lTTgxFFn7TLBHzMo2ylm6WK2raQtikMzewizUbFr_sQ"
)

SHEET_NAME = "MAIN"

HEADERS = [
    "RR Number",
    "Account Name",
    "Billing Address",
    "Company Name",
    "Received Date",
    "Description",
    "Net Weight",
    "Category",
    "Order Type",
    "Remarks",
    "Receiving Notes",
]


def normalize_key(value: Any) -> str:
    return " ".join(
        str(value or "")
        .strip()
        .casefold()
        .split()
    )


def make_row_key(
    row: dict[str, Any],
) -> tuple[str, str]:
    return (
        normalize_key(
            row.get("rr_number")
        ),
        normalize_key(
            row.get("description")
        ),
    )


def get_existing_rr_descriptions(
) -> set[tuple[str, str]]:
    """
    Read MAIN exactly once and return all existing
    RR Number + Description combinations.
    """

    service = get_sheets_service()

    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{SHEET_NAME}'!A2:K",
        )
        .execute()
    )

    existing = set()

    for row in response.get(
        "values",
        [],
    ):
        rr_number = (
            row[0]
            if len(row) > 0
            else ""
        )

        description = (
            row[5]
            if len(row) > 5
            else ""
        )

        if (
            str(rr_number).strip()
            and str(description).strip()
        ):
            existing.add(
                (
                    normalize_key(rr_number),
                    normalize_key(description),
                )
            )

    return existing


def get_first_empty_row() -> int:
    """
    Find the first empty row in column A,
    beginning at row 2.
    """

    service = get_sheets_service()

    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{SHEET_NAME}'!A2:A",
        )
        .execute()
    )

    values = response.get(
        "values",
        [],
    )

    for row_number, row in enumerate(
        values,
        start=2,
    ):
        value = (
            str(row[0]).strip()
            if row
            else ""
        )

        if not value:
            return row_number

    return len(values) + 2


def business_row_to_values(
    row: dict[str, Any],
) -> list[Any]:
    return [
        row.get("rr_number", ""),
        row.get("account_name", ""),
        row.get("billing_address", ""),
        row.get("company_name", ""),
        row.get("received_date", ""),
        row.get("description", ""),
        row.get("net_weight", ""),
        row.get("category", ""),
        row.get("order_type", ""),
        row.get("remarks", ""),
        row.get("receiving_notes", ""),
    ]


def write_rows_bulk(
    rows: list[dict[str, Any]],
) -> int:
    """
    Write many rows in ONE Sheets API request.

    Duplicate filtering should already have been
    performed by the caller.
    """

    if not rows:
        return 0

    service = get_sheets_service()

    start_row = get_first_empty_row()

    end_row = (
        start_row
        + len(rows)
        - 1
    )

    target_range = (
        f"'{SHEET_NAME}'!"
        f"A{start_row}:K{end_row}"
    )

    values = [
        business_row_to_values(row)
        for row in rows
    ]

    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=SPREADSHEET_ID,
            range=target_range,
            valueInputOption="USER_ENTERED",
            body={
                "values": values,
            },
        )
        .execute()
    )

    print(
        f"SHEET BULK WRITE: "
        f"{target_range}"
    )

    return len(values)


def ensure_headers() -> None:
    service = get_sheets_service()

    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{SHEET_NAME}'!A1:K1",
            valueInputOption="RAW",
            body={
                "values": [
                    HEADERS
                ],
            },
        )
        .execute()
    )