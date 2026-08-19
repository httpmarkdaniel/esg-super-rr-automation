"""Deterministic header and description-row extraction for RR PDFs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import pdfplumber

from rr_pdf_consolidator.layout import (
    INPUT_CONTRACT,
    RR_CONTINUATION_PAGE,
    RR_FORM_PAGE,
    LayoutResult,
    bbox_for_tokens,
    extract_tokens,
    find_phrase,
    inspect_layout,
    normalized_bbox,
    normalize_tokens,
)
from rr_pdf_consolidator.table_extract import (
    DETAIL_ROW,
    TOTAL_ROW,
    ExtractedTableRow,
    PageTableSummary,
    extract_page_table,
)


SCHEMA_VERSION = "RR_DESCRIPTION_ROWS_V9"
EXTRACTION_SCOPE = (
    "HEADER_RECEIVING_NOTES_RR_SECTIONS_PIS_GROUPS_AND_DESCRIPTION_ROWS"
)
HEADER_VALUE_VERTICAL_TOLERANCE = 3.0
HEADER_VALUE_MINIMUM_HORIZONTAL_GAP = 2.0
RECEIVING_NOTES_LABEL = "RECEIVING NOTES"


@dataclass(frozen=True)
class HeaderFieldRule:
    """One header value and the recognized label that locates it."""

    name: str
    anchor_name: str
    required: bool = False


@dataclass(frozen=True)
class HeaderFieldEvidence:
    """Extracted value plus its auditable source geometry."""

    field_name: str
    label_anchor: str
    required: bool
    value: str | None
    page_number: int
    label_bbox_points: dict[str, float]
    value_bbox_points: dict[str, float] | None = None
    value_bbox_normalized: dict[str, float] | None = None


@dataclass(frozen=True)
class TableMetadataRule:
    """One RR-level field stored inside the left side of the printed table."""

    field_name: str
    physical_row_number: int
    value_cell: str
    label_cell: str | None = None
    expected_label_tokens: tuple[str, ...] = ()
    label_occurrence: int = 1


@dataclass(frozen=True)
class TableMetadataEvidence:
    """Auditable source row, label, and value for one RR-level table field."""

    field_name: str
    page_number: int
    physical_row_number: int
    value_cell: str
    value: str | None
    raw_value: str | None = None
    label_cell: str | None = None
    expected_label: str | None = None
    source_label: str | None = None
    label_matches: bool | None = None
    selection_rule: str = "EXACT_LABEL"


@dataclass(frozen=True)
class PISGroupEvidence:
    """One PIS marker and the contiguous description rows assigned to it."""

    group_number: int
    pis_no: str | None
    marker_page_number: int
    marker_physical_row_number: int
    start_page_number: int
    start_physical_row_number: int
    end_page_number: int
    end_physical_row_number: int
    description_row_count: int
    association_rule: str


@dataclass(frozen=True)
class RRSectionEvidence:
    """One PIS-based RR section and the metadata assigned to its items."""

    section_number: int
    pis_group_number: int
    identity_page_number: int
    identity_physical_row_number: int
    start_page_number: int
    start_physical_row_number: int
    end_page_number: int
    end_physical_row_number: int
    description_row_count: int
    metadata_values: dict[str, str | None]
    metadata_evidence: list[TableMetadataEvidence]


@dataclass(frozen=True)
class ItemExtractionBoundaryEvidence:
    """The printed total row that deterministically ends item extraction."""

    selection_rule: str
    total_page_number: int
    total_physical_row_number: int
    ignored_post_total_detail_rows: list[dict[str, Any]]


@dataclass(frozen=True)
class DescriptionRecord:
    """One database-grain record for one nonblank description cell."""

    record_number: int
    source_filename: str
    source_sha256: str
    template_id: str
    source_page_number: int
    source_table_row_number: int
    rr_order_type: str | None
    rr_reference_no: str
    rr_received_date: str | None
    rr_job_no: str | None
    rr_schedule_date: str | None
    rr_account_rep: str | None
    rr_table_date: str | None
    rr_enviro_ref_rr_no: str | None
    rr_company_name: str | None
    rr_receiving_notes: str | None
    rr_pis_no: str | None
    rr_expenses: str | None
    rr_manpower: str | None
    rr_trucking_1: str | None
    rr_trucking_2: str | None
    rr_purchased_cost: str | None
    description: str
    kilos: str | None
    less_cage_or_pallets: str | None
    net_weight: str | None
    qty_pcs: str | None
    uom: str | None
    category: str | None
    remarks: str | None


@dataclass
class RRExtractionResult:
    """Machine-readable result of header and description-row extraction."""

    filename: str
    source_path: str
    sha256: str
    input_contract: str = INPUT_CONTRACT
    schema_version: str = SCHEMA_VERSION
    extraction_scope: str = EXTRACTION_SCOPE
    layout_status: str = "NOT_RUN"
    template_id: str | None = None
    status: str = "LAYOUT_REJECTED"
    header_values: dict[str, str | None] = field(default_factory=dict)
    field_evidence: list[HeaderFieldEvidence] = field(default_factory=list)
    table_metadata_values: dict[str, str | None] = field(
        default_factory=dict
    )
    table_metadata_evidence: list[TableMetadataEvidence] = field(
        default_factory=list
    )
    pis_groups: list[PISGroupEvidence] = field(default_factory=list)
    rr_sections: list[RRSectionEvidence] = field(default_factory=list)
    item_extraction_boundary: ItemExtractionBoundaryEvidence | None = None
    description_record_count: int = 0
    description_records: list[DescriptionRecord] = field(default_factory=list)
    page_table_summaries: list[PageTableSummary] = field(default_factory=list)
    table_rows: list[ExtractedTableRow] = field(default_factory=list)
    missing_required_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Compatibility name retained for code written during Step 3A.
HeaderExtractionResult = RRExtractionResult


HEADER_FIELD_RULES = (
    HeaderFieldRule(
        name="order_type",
        anchor_name="order_type_label",
    ),
    HeaderFieldRule(
        name="reference_no",
        anchor_name="reference_no_label",
        required=True,
    ),
    HeaderFieldRule(
        name="received_date",
        anchor_name="received_date_label",
    ),
    HeaderFieldRule(
        name="job_no",
        anchor_name="job_no_label",
    ),
    HeaderFieldRule(
        name="schedule_date",
        anchor_name="schedule_date_label",
    ),
    HeaderFieldRule(
        name="account_rep",
        anchor_name="account_rep_label",
    ),
)

TABLE_METADATA_RULES = (
    TableMetadataRule(
        field_name="rr_table_date",
        physical_row_number=2,
        value_cell="date",
    ),
    TableMetadataRule(
        field_name="rr_enviro_ref_rr_no",
        physical_row_number=2,
        value_cell="enviro_ref_rr_no",
    ),
    TableMetadataRule(
        field_name="rr_company_name",
        physical_row_number=2,
        value_cell="company_name",
    ),
    TableMetadataRule(
        field_name="rr_expenses",
        physical_row_number=4,
        label_cell="date",
        value_cell="enviro_ref_rr_no",
        expected_label_tokens=("EXPENSES",),
    ),
    TableMetadataRule(
        field_name="rr_manpower",
        physical_row_number=5,
        label_cell="date",
        value_cell="enviro_ref_rr_no",
        expected_label_tokens=("MANPOWER",),
    ),
    TableMetadataRule(
        field_name="rr_trucking_1",
        physical_row_number=6,
        label_cell="date",
        value_cell="enviro_ref_rr_no",
        expected_label_tokens=("TRUCKING",),
    ),
    TableMetadataRule(
        field_name="rr_trucking_2",
        physical_row_number=7,
        label_cell="date",
        value_cell="enviro_ref_rr_no",
        expected_label_tokens=("TRUCKING",),
        label_occurrence=2,
    ),
    TableMetadataRule(
        field_name="rr_purchased_cost",
        physical_row_number=8,
        label_cell="date",
        value_cell="enviro_ref_rr_no",
        expected_label_tokens=("PURCHASED", "COST"),
    ),
)

GLOBAL_RR_FIELDS = {
    "rr_expenses",
    "rr_manpower",
    "rr_trucking_1",
    "rr_trucking_2",
    "rr_purchased_cost",
    "rr_receiving_notes",
}
SECTION_DIRECT_FIELDS = (
    ("rr_table_date", "date"),
    ("rr_enviro_ref_rr_no", "enviro_ref_rr_no"),
    ("rr_company_name", "company_name"),
)


def word_bbox(word: dict[str, Any]) -> dict[str, float]:
    """Return a stable point bounding box for one pdfplumber word."""

    return {
        "x0": round(float(word["x0"]), 3),
        "top": round(float(word["top"]), 3),
        "x1": round(float(word["x1"]), 3),
        "bottom": round(float(word["bottom"]), 3),
    }


def words_right_of_label(
    words: list[dict[str, Any]],
    label_bbox: dict[str, float],
    *,
    vertical_tolerance: float = HEADER_VALUE_VERTICAL_TOLERANCE,
    horizontal_gap: float = HEADER_VALUE_MINIMUM_HORIZONTAL_GAP,
) -> list[dict[str, Any]]:
    """Select words to the right of a label on the same printed baseline."""

    label_center = (
        float(label_bbox["top"]) + float(label_bbox["bottom"])
    ) / 2.0
    minimum_x = float(label_bbox["x1"]) + horizontal_gap

    candidates: list[dict[str, Any]] = []
    for word in words:
        word_center = (
            float(word["top"]) + float(word["bottom"])
        ) / 2.0
        if float(word["x0"]) < minimum_x:
            continue
        if abs(word_center - label_center) > vertical_tolerance:
            continue
        candidates.append(word)

    return sorted(
        candidates,
        key=lambda word: (
            float(word["x0"]),
            float(word["top"]),
        ),
    )


def bbox_for_words(
    words: list[dict[str, Any]],
    page_width: float,
    page_height: float,
) -> tuple[dict[str, float], dict[str, float]]:
    """Return point and normalized bounding boxes for selected words."""

    x0 = min(float(word["x0"]) for word in words)
    top = min(float(word["top"]) for word in words)
    x1 = max(float(word["x1"]) for word in words)
    bottom = max(float(word["bottom"]) for word in words)
    points = {
        "x0": round(x0, 3),
        "top": round(top, 3),
        "x1": round(x1, 3),
        "bottom": round(bottom, 3),
    }
    return points, normalized_bbox(
        x0,
        top,
        x1,
        bottom,
        page_width,
        page_height,
    )


def extract_field_evidence(
    *,
    words: list[dict[str, Any]],
    rule: HeaderFieldRule,
    anchor: Any,
    page_number: int,
    page_width: float,
    page_height: float,
) -> HeaderFieldEvidence:
    """Extract one header value from the same line as its matched label."""

    if anchor.bbox_points is None:
        raise ValueError(f"Anchor has no geometry: {rule.anchor_name}")

    value_words = words_right_of_label(words, anchor.bbox_points)
    value = None
    value_points = None
    value_normalized = None
    if value_words:
        value = " ".join(str(word["text"]).strip() for word in value_words)
        value_points, value_normalized = bbox_for_words(
            value_words,
            page_width,
            page_height,
        )

    return HeaderFieldEvidence(
        field_name=rule.name,
        label_anchor=rule.anchor_name,
        required=rule.required,
        value=value,
        page_number=page_number,
        label_bbox_points=dict(anchor.bbox_points),
        value_bbox_points=value_points,
        value_bbox_normalized=value_normalized,
    )


def first_form_page(layout: LayoutResult) -> Any | None:
    """Return the single recognized RR form page."""

    return next(
        (
            page
            for page in layout.pages
            if page.page_role == RR_FORM_PAGE
        ),
        None,
    )


def table_row_cell(
    row: ExtractedTableRow,
    cell_name: str,
) -> str | None:
    """Read a high-level table cell or one COMPANY NAME form subcell."""

    if cell_name in row.company_form_subcells:
        return row.company_form_subcells[cell_name]
    return row.cells.get(cell_name)


def clean_source_value(value: str | None) -> str | None:
    """Normalize only surrounding whitespace; preserve printed content."""

    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def date_token_from_cell(value: str | None) -> str | None:
    """Return the printed date token, including from a DAY N wrapper."""

    cleaned = clean_source_value(value)
    if cleaned is None:
        return None
    match = re.search(
        r"(?<!\d)(\d{1,2}/\d{1,2}/(?:\d{2}|\d{4}))(?!\d)",
        cleaned,
    )
    return match.group(1) if match is not None else None


def reconcile_form_identity_boundary_spill(
    *,
    words: list[dict[str, Any]],
    rows: list[ExtractedTableRow],
    form_page_number: int,
    company_boundary: float,
    header_reference: str,
) -> dict[str, str] | None:
    """Recover a company prefix proven to cross the printed cell boundary."""

    identity_rows = [
        row
        for row in rows
        if row.page_number == form_page_number
        and date_token_from_cell(row.cells.get("date")) is not None
        and clean_source_value(
            row.cells.get("enviro_ref_rr_no")
        )
        is not None
        and clean_source_value(row.cells.get("company_name")) is not None
    ]
    if not identity_rows:
        return None

    identity_row = identity_rows[0]
    raw_reference = clean_source_value(
        identity_row.cells.get("enviro_ref_rr_no")
    )
    raw_company = clean_source_value(
        identity_row.cells.get("company_name")
    )
    if (
        raw_reference is None
        or raw_company is None
        or raw_reference == header_reference
        or not raw_reference.startswith(header_reference)
    ):
        return None

    company_prefix = raw_reference[len(header_reference) :].strip()
    if not company_prefix or not normalize_tokens(company_prefix):
        return None

    row_top = float(identity_row.bbox_points["top"])
    row_bottom = float(identity_row.bbox_points["bottom"])
    crossing_word_found = any(
        str(word["text"]).strip() == raw_reference
        and row_top
        <= (
            (float(word["top"]) + float(word["bottom"])) / 2.0
        )
        <= row_bottom
        and float(word["x0"]) < company_boundary < float(word["x1"])
        for word in words
    )
    if not crossing_word_found:
        return None

    repaired_company = f"{company_prefix} {raw_company}"
    identity_row.cells["enviro_ref_rr_no"] = header_reference
    identity_row.cells["company_name"] = repaired_company
    return {
        "raw_reference": raw_reference,
        "raw_company": raw_company,
        "repaired_reference": header_reference,
        "repaired_company": repaired_company,
    }


def extract_receiving_notes(
    pages: list[Any],
) -> tuple[str | None, list[TableMetadataEvidence]]:
    """Extract the RR-wide note from the exact label's printed baseline."""

    values: list[str] = []
    evidence_items: list[TableMetadataEvidence] = []

    for page_number, page in enumerate(pages, start=1):
        page_width = float(page.width)
        page_height = float(page.height)
        tokens = extract_tokens(page)
        label_tokens = find_phrase(
            tokens,
            RECEIVING_NOTES_LABEL,
            (0.0, 0.0, page_width, page_height),
        )
        if label_tokens is None:
            continue

        label_points, _ = bbox_for_tokens(
            label_tokens,
            page_width,
            page_height,
        )
        words = page.extract_words(
            keep_blank_chars=False,
            use_text_flow=False,
        )
        value_words = words_right_of_label(words, label_points)
        value = clean_source_value(
            " ".join(
                str(word["text"]).strip()
                for word in value_words
            )
        )
        if value is not None:
            values.append(value)
        evidence_items.append(
            TableMetadataEvidence(
                field_name="rr_receiving_notes",
                page_number=page_number,
                physical_row_number=0,
                value_cell="same_baseline_right_of_label",
                value=value,
                label_cell="page_text",
                expected_label=RECEIVING_NOTES_LABEL,
                source_label=RECEIVING_NOTES_LABEL,
                label_matches=True,
                selection_rule=(
                    "EXACT_LABEL_SAME_BASELINE_VALUE"
                ),
            )
        )

    distinct_values = list(dict.fromkeys(values))
    if len(distinct_values) > 1:
        raise ValueError(
            "Conflicting RECEIVING NOTES values were printed in one PDF: "
            + ", ".join(repr(value) for value in distinct_values)
        )
    return (
        distinct_values[0] if distinct_values else None,
        evidence_items,
    )


def extract_table_metadata(
    rows: list[ExtractedTableRow],
    *,
    form_page_number: int,
) -> tuple[
    dict[str, str | None],
    list[TableMetadataEvidence],
]:
    """Extract the embedded RR form once from the first form-page rows."""

    rows_by_number = {
        row.physical_row_number: row
        for row in rows
        if row.page_number == form_page_number
    }
    values: dict[str, str | None] = {}
    evidence_items: list[TableMetadataEvidence] = []
    label_errors: list[str] = []

    for rule in TABLE_METADATA_RULES:
        selection_rule = "FIXED_PHYSICAL_ROW"
        if rule.label_cell is None:
            row = rows_by_number.get(rule.physical_row_number)
        else:
            selection_rule = "EXACT_LABEL_OCCURRENCE"
            matching_rows = [
                row
                for row in rows_by_number.values()
                if tuple(
                    normalize_tokens(
                        table_row_cell(row, rule.label_cell) or ""
                    )
                )
                == rule.expected_label_tokens
            ]
            row = (
                matching_rows[rule.label_occurrence - 1]
                if len(matching_rows) >= rule.label_occurrence
                else None
            )
        if row is None:
            raise ValueError(
                f"Missing form-page source row for {rule.field_name}."
            )

        value = clean_source_value(table_row_cell(row, rule.value_cell))
        source_label = (
            clean_source_value(table_row_cell(row, rule.label_cell))
            if rule.label_cell is not None
            else None
        )
        expected_label = (
            " ".join(rule.expected_label_tokens)
            if rule.expected_label_tokens
            else None
        )
        label_matches = None
        if rule.label_cell is not None:
            label_matches = (
                tuple(normalize_tokens(source_label or ""))
                == rule.expected_label_tokens
            )
            if not label_matches:
                label_errors.append(
                    f"{rule.field_name} expected label "
                    f"{expected_label!r} in row "
                    f"{row.physical_row_number} cell "
                    f"{rule.label_cell!r}, found {source_label!r}"
                )

        values[rule.field_name] = value
        evidence_items.append(
            TableMetadataEvidence(
                field_name=rule.field_name,
                page_number=form_page_number,
                physical_row_number=row.physical_row_number,
                value_cell=rule.value_cell,
                value=value,
                label_cell=rule.label_cell,
                expected_label=expected_label,
                source_label=source_label,
                label_matches=label_matches,
                selection_rule=selection_rule,
            )
        )

    if label_errors:
        raise ValueError(
            "Embedded RR metadata labels do not match the template: "
            + "; ".join(label_errors)
        )
    return values, evidence_items


def is_pis_marker(row: ExtractedTableRow) -> bool:
    """Return True only for an exact normalized PIS NO label."""

    label = row.company_form_subcells.get("company_form_label")
    return normalize_tokens(label or "") == ["PIS", "NO"]


def select_item_region_rows(
    rows: list[ExtractedTableRow],
) -> tuple[
    list[ExtractedTableRow],
    ItemExtractionBoundaryEvidence | None,
]:
    """Limit item interpretation to rows at or before one printed total.

    All physical rows remain in the extraction result as audit evidence. If
    the document does not contain exactly one total row, no boundary is
    inferred here; the existing validation control will quarantine it.
    """

    ordered_rows = sorted(
        rows,
        key=lambda row: (
            row.page_number,
            row.physical_row_number,
        ),
    )
    total_rows = [
        row for row in ordered_rows if row.row_type == TOTAL_ROW
    ]
    if len(total_rows) != 1:
        return ordered_rows, None

    total_row = total_rows[0]
    total_coordinate = (
        total_row.page_number,
        total_row.physical_row_number,
    )
    item_region_rows = [
        row
        for row in ordered_rows
        if (row.page_number, row.physical_row_number)
        <= total_coordinate
    ]
    ignored_rows = [
        {
            "page_number": row.page_number,
            "physical_row_number": row.physical_row_number,
            "description": clean_source_value(
                row.cells.get("description")
            ),
        }
        for row in ordered_rows
        if row.row_type == DETAIL_ROW
        and (row.page_number, row.physical_row_number)
        > total_coordinate
    ]
    return (
        item_region_rows,
        ItemExtractionBoundaryEvidence(
            selection_rule="UNIQUE_PRINTED_TOTAL_ROW_EXCLUSIVE_END",
            total_page_number=total_row.page_number,
            total_physical_row_number=(
                total_row.physical_row_number
            ),
            ignored_post_total_detail_rows=ignored_rows,
        ),
    )


def associate_pis_numbers(
    rows: list[ExtractedTableRow],
) -> tuple[
    dict[tuple[int, int], str | None],
    list[PISGroupEvidence],
]:
    """Assign PIS numbers to item rows using explicit printed transitions.

    The first PIS marker starts one physical row earlier when that row is an
    item. A later marker starts on its own item row, on the immediately
    preceding item when its own description is blank, or at the next item
    when neither adjacent row supplies one. The next PIS marker ends the
    preceding group. Other non-description rows are skipped.
    """

    ordered_rows = sorted(
        rows,
        key=lambda row: (
            row.page_number,
            row.physical_row_number,
        ),
    )
    assignments: dict[tuple[int, int], str | None] = {}
    groups: list[PISGroupEvidence] = []
    active_group: dict[str, Any] | None = None
    marker_count = 0

    def row_key(row: ExtractedTableRow) -> tuple[int, int]:
        return row.page_number, row.physical_row_number

    def add_description(
        group: dict[str, Any],
        row: ExtractedTableRow,
    ) -> None:
        key = row_key(row)
        if key in assignments and assignments[key] != group["pis_no"]:
            raise ValueError(
                f"Description row page {key[0]} row {key[1]} received "
                f"conflicting PIS numbers {assignments[key]!r} and "
                f"{group['pis_no']!r}."
            )
        if group["start_row"] is None:
            group["start_row"] = row
        assignments[key] = group["pis_no"]
        group["end_row"] = row
        group["description_row_count"] += 1
        group["description_rows"].append(row)

    def detach_last_description(
        group: dict[str, Any],
        row: ExtractedTableRow,
    ) -> None:
        key = row_key(row)
        description_rows = group["description_rows"]
        if not description_rows or row_key(description_rows[-1]) != key:
            return
        description_rows.pop()
        assignments.pop(key, None)
        group["description_row_count"] -= 1
        if description_rows:
            group["start_row"] = description_rows[0]
            group["end_row"] = description_rows[-1]
        else:
            group["start_row"] = None
            group["end_row"] = None

    def finalize_group() -> None:
        nonlocal active_group
        if active_group is None:
            return
        if active_group["description_row_count"] == 0:
            active_group = None
            return
        start_row = active_group["start_row"]
        end_row = active_group["end_row"]
        if start_row is None or end_row is None:
            raise AssertionError(
                "A nonempty PIS group must have start and end rows."
            )
        groups.append(
            PISGroupEvidence(
                group_number=active_group["group_number"],
                pis_no=active_group["pis_no"],
                marker_page_number=active_group["marker"].page_number,
                marker_physical_row_number=(
                    active_group["marker"].physical_row_number
                ),
                start_page_number=start_row.page_number,
                start_physical_row_number=(
                    start_row.physical_row_number
                ),
                end_page_number=end_row.page_number,
                end_physical_row_number=end_row.physical_row_number,
                description_row_count=active_group[
                    "description_row_count"
                ],
                association_rule=active_group["association_rule"],
            )
        )
        active_group = None

    for index, row in enumerate(ordered_rows):
        if is_pis_marker(row):
            marker_count += 1
            pis_no = clean_source_value(
                row.company_form_subcells.get("company_form_value")
            )

            if marker_count == 1:
                preceding_row = (
                    ordered_rows[index - 1] if index > 0 else None
                )
                if (
                    preceding_row is not None
                    and preceding_row.page_number == row.page_number
                    and preceding_row.physical_row_number
                    == row.physical_row_number - 1
                    and preceding_row.row_type == DETAIL_ROW
                ):
                    start_row = preceding_row
                    association_rule = (
                        "FIRST_MARKER_STARTS_PREVIOUS_ROW"
                    )
                elif row.row_type == DETAIL_ROW:
                    start_row = row
                    association_rule = (
                        "FIRST_MARKER_STARTS_SAME_ROW_WHEN_PREVIOUS_BLANK"
                    )
                else:
                    raise ValueError(
                        "The first PIS marker has no item description on "
                        "either its own row or the row immediately above."
                    )
            else:
                if row.row_type == DETAIL_ROW:
                    start_row = row
                    association_rule = "LATER_MARKER_STARTS_SAME_ROW"
                else:
                    preceding_row = (
                        ordered_rows[index - 1] if index > 0 else None
                    )
                    if (
                        preceding_row is not None
                        and preceding_row.page_number == row.page_number
                        and preceding_row.physical_row_number
                        == row.physical_row_number - 1
                        and preceding_row.row_type == DETAIL_ROW
                    ):
                        start_row = preceding_row
                        association_rule = (
                            "LATER_MARKER_STARTS_PREVIOUS_ROW"
                        )
                        if active_group is not None:
                            detach_last_description(
                                active_group,
                                preceding_row,
                            )
                    else:
                        start_row = None
                        association_rule = (
                            "LATER_MARKER_STARTS_NEXT_DESCRIPTION"
                        )

            finalize_group()

            active_group = {
                "group_number": marker_count,
                "pis_no": pis_no,
                "marker": row,
                "start_row": start_row,
                "end_row": start_row,
                "description_row_count": 0,
                "description_rows": [],
                "association_rule": association_rule,
            }
            if start_row is not None and start_row is not row:
                add_description(active_group, start_row)
            if row.row_type == DETAIL_ROW:
                add_description(active_group, row)
            continue

        if row.row_type == DETAIL_ROW:
            if active_group is not None:
                add_description(active_group, row)
            continue

    finalize_group()

    detail_keys = {
        row_key(row)
        for row in ordered_rows
        if row.row_type == DETAIL_ROW
    }
    unassigned = sorted(detail_keys - assignments.keys())
    if unassigned:
        rendered = ", ".join(
            f"page {page_number} row {row_number}"
            for page_number, row_number in unassigned
        )
        raise ValueError(
            "Item descriptions without an unambiguous PIS assignment: "
            + rendered
        )
    if not groups:
        raise ValueError("No PIS groups were found.")
    return assignments, groups


def build_rr_sections(
    rows: list[ExtractedTableRow],
    pis_groups: list[PISGroupEvidence],
    *,
    global_values: dict[str, str | None],
    global_evidence: list[TableMetadataEvidence],
) -> tuple[
    list[RRSectionEvidence],
    dict[tuple[int, int], dict[str, str | None]],
]:
    """Build section-level metadata and map it to every description row."""

    ordered_rows = sorted(
        rows,
        key=lambda row: (
            row.page_number,
            row.physical_row_number,
        ),
    )
    index_by_coordinate = {
        (row.page_number, row.physical_row_number): index
        for index, row in enumerate(ordered_rows)
    }
    sections: list[RRSectionEvidence] = []
    values_by_description: dict[
        tuple[int, int],
        dict[str, str | None],
    ] = {}

    for group in pis_groups:
        marker_coordinate = (
            group.marker_page_number,
            group.marker_physical_row_number,
        )
        marker_index = index_by_coordinate[marker_coordinate]
        preceding_rows = ordered_rows[: marker_index + 1]
        identity_candidates = [
            row
            for row in preceding_rows
            if clean_source_value(
                row.cells.get("enviro_ref_rr_no")
            )
            is not None
            and normalize_tokens(
                row.cells.get("enviro_ref_rr_no") or ""
            )
            != ["ENVIRO", "REF", "RR", "NO"]
        ]
        if not identity_candidates:
            raise ValueError(
                f"PIS group {group.group_number} has no preceding RR "
                "identity row."
            )
        identity_row = identity_candidates[-1]

        section_values = dict(global_values)
        section_evidence = [
            item
            for item in global_evidence
            if item.field_name in GLOBAL_RR_FIELDS
        ]
        date_candidates = [
            (row, date_token_from_cell(row.cells.get("date")))
            for row in preceding_rows
            if date_token_from_cell(row.cells.get("date")) is not None
        ]
        company_candidates = [
            row
            for row in preceding_rows
            if clean_source_value(
                row.cells.get("enviro_ref_rr_no")
            )
            is not None
            and clean_source_value(row.cells.get("company_name"))
            is not None
            and normalize_tokens(
                row.cells.get("enviro_ref_rr_no") or ""
            )
            != ["ENVIRO", "REF", "RR", "NO"]
        ]
        if not date_candidates or not company_candidates:
            raise ValueError(
                f"PIS group {group.group_number} has incomplete preceding "
                "RR identity metadata."
            )
        direct_sources = {
            "rr_table_date": (
                date_candidates[-1][0],
                date_candidates[-1][1],
                "LATEST_PRINTED_DATE_BEFORE_PIS",
            ),
            "rr_enviro_ref_rr_no": (
                identity_row,
                clean_source_value(
                    identity_row.cells.get("enviro_ref_rr_no")
                ),
                "LATEST_REFERENCE_ROW_BEFORE_PIS",
            ),
            "rr_company_name": (
                company_candidates[-1],
                clean_source_value(
                    company_candidates[-1].cells.get("company_name")
                ),
                "LATEST_COMPANY_IDENTITY_BEFORE_PIS",
            ),
        }
        for field_name, cell_name in SECTION_DIRECT_FIELDS:
            source_row, value, selection_rule = direct_sources[field_name]
            section_values[field_name] = value
            section_evidence.append(
                TableMetadataEvidence(
                    field_name=field_name,
                    page_number=source_row.page_number,
                    physical_row_number=(
                        source_row.physical_row_number
                    ),
                    value_cell=cell_name,
                    value=value,
                    selection_rule=selection_rule,
                )
            )

        marker_row = ordered_rows[marker_index]
        section_values["rr_pis_no"] = group.pis_no
        section_evidence.append(
            TableMetadataEvidence(
                field_name="rr_pis_no",
                page_number=marker_row.page_number,
                physical_row_number=marker_row.physical_row_number,
                value_cell="company_form_value",
                value=group.pis_no,
                label_cell="company_form_label",
                expected_label="PIS NO",
                source_label=clean_source_value(
                    marker_row.company_form_subcells.get(
                        "company_form_label"
                    )
                ),
                label_matches=True,
                selection_rule="PIS_GROUP_MARKER",
            )
        )

        start_coordinate = (
            group.start_page_number,
            group.start_physical_row_number,
        )
        end_coordinate = (
            group.end_page_number,
            group.end_physical_row_number,
        )
        assigned_rows = [
            row
            for row in ordered_rows
            if row.row_type == DETAIL_ROW
            and start_coordinate
            <= (row.page_number, row.physical_row_number)
            <= end_coordinate
        ]
        if len(assigned_rows) != group.description_row_count:
            raise ValueError(
                f"PIS group {group.group_number} expected "
                f"{group.description_row_count} description row(s), "
                f"found {len(assigned_rows)}."
            )
        for row in assigned_rows:
            coordinate = (
                row.page_number,
                row.physical_row_number,
            )
            if coordinate in values_by_description:
                raise ValueError(
                    f"Description row page {coordinate[0]} row "
                    f"{coordinate[1]} belongs to overlapping RR sections."
                )
            values_by_description[coordinate] = section_values

        sections.append(
            RRSectionEvidence(
                section_number=group.group_number,
                pis_group_number=group.group_number,
                identity_page_number=identity_row.page_number,
                identity_physical_row_number=(
                    identity_row.physical_row_number
                ),
                start_page_number=group.start_page_number,
                start_physical_row_number=(
                    group.start_physical_row_number
                ),
                end_page_number=group.end_page_number,
                end_physical_row_number=group.end_physical_row_number,
                description_row_count=group.description_row_count,
                metadata_values=section_values,
                metadata_evidence=section_evidence,
            )
        )

    detail_coordinates = {
        (row.page_number, row.physical_row_number)
        for row in ordered_rows
        if row.row_type == DETAIL_ROW
    }
    unassigned = sorted(
        detail_coordinates - values_by_description.keys()
    )
    if unassigned:
        raise ValueError(
            "Description rows without RR-section metadata: "
            + ", ".join(
                f"page {page} row {row}"
                for page, row in unassigned
            )
        )
    return sections, values_by_description


def build_description_record(
    *,
    record_number: int,
    row: ExtractedTableRow,
    header_values: dict[str, str | None],
    table_metadata_values: dict[str, str | None],
    pis_no: str | None,
    filename: str,
    sha256: str,
    template_id: str,
) -> DescriptionRecord:
    """Combine one item row with both document-level RR metadata sources."""

    reference_no = header_values.get("reference_no")
    description = row.cells.get("description")
    if reference_no is None:
        raise ValueError("Cannot build a record without reference_no.")
    if description is None:
        raise ValueError("Cannot build a record without description.")

    return DescriptionRecord(
        record_number=record_number,
        source_filename=filename,
        source_sha256=sha256,
        template_id=template_id,
        source_page_number=row.page_number,
        source_table_row_number=row.physical_row_number,
        rr_order_type=header_values.get("order_type"),
        rr_reference_no=reference_no,
        rr_received_date=header_values.get("received_date"),
        rr_job_no=header_values.get("job_no"),
        rr_schedule_date=header_values.get("schedule_date"),
        rr_account_rep=header_values.get("account_rep"),
        rr_table_date=table_metadata_values.get("rr_table_date"),
        rr_enviro_ref_rr_no=table_metadata_values.get(
            "rr_enviro_ref_rr_no"
        ),
        rr_company_name=table_metadata_values.get("rr_company_name"),
        rr_receiving_notes=table_metadata_values.get(
            "rr_receiving_notes"
        ),
        rr_pis_no=pis_no,
        rr_expenses=table_metadata_values.get("rr_expenses"),
        rr_manpower=table_metadata_values.get("rr_manpower"),
        rr_trucking_1=table_metadata_values.get("rr_trucking_1"),
        rr_trucking_2=table_metadata_values.get("rr_trucking_2"),
        rr_purchased_cost=table_metadata_values.get("rr_purchased_cost"),
        description=description,
        kilos=row.cells.get("kilos"),
        less_cage_or_pallets=row.cells.get("less_cage_or_pallets"),
        net_weight=row.cells.get("net_weight"),
        qty_pcs=row.cells.get("qty_pcs"),
        uom=row.cells.get("uom"),
        category=row.cells.get("category"),
        remarks=row.cells.get("remarks"),
    )


def inspect_rr(path: Path) -> RRExtractionResult:
    """Run upstream gates, then extract headers and description-grain rows."""

    layout = inspect_layout(path)
    result = RRExtractionResult(
        filename=layout.filename,
        source_path=layout.source_path,
        sha256=layout.sha256,
        layout_status=layout.status,
        template_id=layout.template_id,
        warnings=list(layout.preflight_warnings),
    )
    if layout.status != "LAYOUT_RECOGNIZED":
        result.status = "LAYOUT_REJECTED"
        result.errors.extend(layout.errors)
        return result

    form_page = first_form_page(layout)
    if form_page is None:
        result.status = "RR_EXTRACTION_FAILED"
        result.errors.append("No recognized RR form page is available.")
        return result

    anchors = {anchor.name: anchor for anchor in form_page.anchors}
    try:
        with pdfplumber.open(layout.source_path) as pdf:
            page = pdf.pages[form_page.page_number - 1]
            words = page.extract_words(
                keep_blank_chars=False,
                use_text_flow=False,
            )
            words = sorted(
                words,
                key=lambda word: (
                    float(word["top"]),
                    float(word["x0"]),
                ),
            )

            for rule in HEADER_FIELD_RULES:
                anchor = anchors.get(rule.anchor_name)
                if anchor is None or not anchor.found:
                    result.errors.append(
                        f"Required layout anchor is unavailable: "
                        f"{rule.anchor_name}."
                    )
                    continue

                evidence = extract_field_evidence(
                    words=words,
                    rule=rule,
                    anchor=anchor,
                    page_number=form_page.page_number,
                    page_width=float(page.width),
                    page_height=float(page.height),
                )
                result.field_evidence.append(evidence)
                result.header_values[rule.name] = evidence.value
                if rule.required and evidence.value is None:
                    result.missing_required_fields.append(rule.name)

            if result.errors or result.missing_required_fields:
                result.status = "RR_EXTRACTION_FAILED"
                if result.missing_required_fields:
                    result.errors.append(
                        "Missing required header values: "
                        + ", ".join(result.missing_required_fields)
                    )
                return result

            supported_roles = {
                RR_FORM_PAGE,
                RR_CONTINUATION_PAGE,
            }
            for page_layout in layout.pages:
                if page_layout.page_role not in supported_roles:
                    continue
                page = pdf.pages[page_layout.page_number - 1]
                rows, summary = extract_page_table(page, page_layout)
                result.table_rows.extend(rows)
                result.page_table_summaries.append(summary)

            if layout.template_id is None:
                raise ValueError("Recognized layout has no template ID.")
            if (
                form_page.table is None
                or len(form_page.table.column_boundaries_points)
                < 4
            ):
                raise ValueError(
                    "Recognized form page has incomplete table boundaries."
                )
            header_reference = result.header_values.get("reference_no")
            if header_reference is None:
                raise ValueError(
                    "Cannot reconcile table identity without reference_no."
                )

            identity_repair = reconcile_form_identity_boundary_spill(
                words=words,
                rows=result.table_rows,
                form_page_number=form_page.page_number,
                company_boundary=(
                    form_page.table.column_boundaries_points[2]
                ),
                header_reference=header_reference,
            )

            (
                result.table_metadata_values,
                result.table_metadata_evidence,
            ) = extract_table_metadata(
                result.table_rows,
                form_page_number=form_page.page_number,
            )
            if identity_repair is not None:
                result.table_metadata_evidence = [
                    replace(
                        evidence,
                        raw_value=(
                            identity_repair["raw_reference"]
                            if evidence.field_name
                            == "rr_enviro_ref_rr_no"
                            else identity_repair["raw_company"]
                        ),
                        selection_rule=(
                            "HEADER_REFERENCE_AND_CROSS_BOUNDARY_"
                            "COMPANY_PREFIX_RECOVERY"
                        ),
                    )
                    if evidence.field_name
                    in {
                        "rr_enviro_ref_rr_no",
                        "rr_company_name",
                    }
                    else evidence
                    for evidence in result.table_metadata_evidence
                ]
                result.warnings.append(
                    "Recovered a company-name prefix from a PDF word "
                    "that began with the exact header reference and "
                    "crossed the Enviro Ref./COMPANY NAME boundary."
                )
            receiving_notes, receiving_notes_evidence = (
                extract_receiving_notes(list(pdf.pages))
            )
            result.table_metadata_values["rr_receiving_notes"] = (
                receiving_notes
            )
            result.table_metadata_evidence.extend(
                receiving_notes_evidence
            )
            (
                item_region_rows,
                result.item_extraction_boundary,
            ) = select_item_region_rows(result.table_rows)
            item_boundary = result.item_extraction_boundary
            ignored_post_total_rows = (
                item_boundary.ignored_post_total_detail_rows
                if item_boundary is not None
                else []
            )
            if ignored_post_total_rows:
                ignored_count = len(ignored_post_total_rows)
                result.warnings.append(
                    f"Ignored {ignored_count} description-shaped row(s) "
                    "after the unique printed total row."
                )
            pis_assignments, result.pis_groups = associate_pis_numbers(
                item_region_rows
            )
            (
                result.rr_sections,
                section_values_by_description,
            ) = build_rr_sections(
                item_region_rows,
                result.pis_groups,
                global_values=result.table_metadata_values,
                global_evidence=result.table_metadata_evidence,
            )

            detail_rows = [
                row
                for row in item_region_rows
                if row.row_type == DETAIL_ROW
            ]
            for record_number, row in enumerate(detail_rows, start=1):
                result.description_records.append(
                    build_description_record(
                        record_number=record_number,
                        row=row,
                        header_values=result.header_values,
                        table_metadata_values=(
                            section_values_by_description[
                                (
                                    row.page_number,
                                    row.physical_row_number,
                                )
                            ]
                        ),
                        pis_no=pis_assignments[
                            (
                                row.page_number,
                                row.physical_row_number,
                            )
                        ],
                        filename=result.filename,
                        sha256=result.sha256,
                        template_id=layout.template_id,
                    )
                )
            result.description_record_count = len(
                result.description_records
            )
    except Exception as error:
        result.status = "RR_EXTRACTION_FAILED"
        result.errors.append(f"{type(error).__name__}: {error}")
        return result

    if not result.description_records:
        result.status = "RR_EXTRACTION_FAILED"
        result.errors.append(
            "No nonblank description rows were extracted."
        )
        return result

    result.status = "RR_EXTRACTION_SUCCEEDED"
    return result


def inspect_header(path: Path) -> RRExtractionResult:
    """Compatibility wrapper; Step 3A now continues through Step 3B."""

    return inspect_rr(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic RR header and description-row extraction."
        )
    )
    parser.add_argument("pdf", type=Path, help="Path to the PDF to extract.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the JSON result. Otherwise prints to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result = inspect_rr(args.pdf)
    except (FileNotFoundError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1

    output_text = json.dumps(result.to_dict(), indent=2, sort_keys=True)
    if args.output:
        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text + "\n", encoding="utf-8")
        print(f"RR extraction result written to: {output_path}")
    else:
        print(output_text)

    return 0 if result.status == "RR_EXTRACTION_SUCCEEDED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
