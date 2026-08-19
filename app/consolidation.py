from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
import unicodedata

ADDITIVE_FIELDS = (
    "net_weight",
)

def normalize_display(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def normalization_key(value: str) -> str:
    return normalize_display(value).casefold()


def to_decimal(value: Any) -> Decimal:
    text = str(value or "").replace(",", "").strip().lstrip("'")
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Non-numeric additive value: {value!r}") from exc


def format_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def consolidate_records(records: Iterable[Any]) -> list[dict[str, Any]]:
    """Consolidate repeated item descriptions within the same RR.

    Grain: (RR reference number, normalized description).
    Numeric transactional fields are summed. Non-additive fields are kept from
    the first row and conflicts are surfaced explicitly rather than hidden.
    """
    grouped: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()

    for record in records:
        row = record if isinstance(record, dict) else vars(record)
        rr_number = str(row.get("rr_reference_no") or "").strip()
        description = str(row.get("description") or "").strip()
        if not rr_number or not description:
            continue

        display = normalize_display(description)
        key = (rr_number, normalization_key(description))

        if key not in grouped:
            item = dict(row)
            item["description"] = display
            item["normalized_description"] = key[1]
            item["source_rows"] = [
                {
                    "page": row.get("source_page_number"),
                    "row": row.get("source_table_row_number"),
                }
            ]
            item["occurrence_count"] = 1
            item["conflicts"] = []
            for field in ADDITIVE_FIELDS:
                item[field] = to_decimal(row.get(field))
            grouped[key] = item
            continue

        item = grouped[key]
        item["occurrence_count"] += 1
        item["source_rows"].append(
            {
                "page": row.get("source_page_number"),
                "row": row.get("source_table_row_number"),
            }
        )
        for field in ADDITIVE_FIELDS:
            item[field] += to_decimal(row.get(field))

            field = "category"
            old = str(item.get(field) or "").strip()
            new = str(row.get(field) or "").strip()
            if old and new and old.casefold() != new.casefold():
                conflict = {"field": field, "values": sorted({old, new})}
                if conflict not in item["conflicts"]:
                    item["conflicts"].append(conflict)
            elif not old and new:
                item[field] = new

    output: list[dict[str, Any]] = []
    for item in grouped.values():
        for field in ADDITIVE_FIELDS:
            item[field] = format_decimal(item[field])
        output.append(item)
    return output
