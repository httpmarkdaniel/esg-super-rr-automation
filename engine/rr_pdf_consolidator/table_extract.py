"""Deterministic ruled-table extraction for RR description rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rr_pdf_consolidator.layout import (
    PageLayoutResult,
    TableLayout,
    cluster_positions,
    normalize_tokens,
)


HORIZONTAL_LINE_TOLERANCE = 0.5
VERTICAL_LINE_TOLERANCE = 0.5
TABLE_EDGE_TOLERANCE = 2.0
ROW_BOUNDARY_CLUSTER_TOLERANCE = 1.0
COLUMN_BOUNDARY_CLUSTER_TOLERANCE = 1.0
MINIMUM_SUBCOLUMN_OVERLAP_POINTS = 20.0
MINIMUM_SUBCOLUMN_OVERLAP_RATIO = 0.25

HEADER_ROW = "HEADER_ROW"
DETAIL_ROW = "DETAIL_ROW"
TOTAL_ROW = "TOTAL_ROW"
BLANK_ROW = "BLANK_ROW"
AUXILIARY_ROW = "AUXILIARY_ROW"

TABLE_COLUMNS = (
    "date",
    "enviro_ref_rr_no",
    "company_name",
    "description",
    "kilos",
    "less_cage_or_pallets",
    "net_weight",
    "qty_pcs",
    "uom",
    "category",
    "remarks",
)

NUMERIC_TOTAL_COLUMNS = (
    "kilos",
    "less_cage_or_pallets",
    "net_weight",
    "qty_pcs",
)

REQUIRED_TOTAL_COLUMNS = (
    "kilos",
    "net_weight",
    "qty_pcs",
)

NON_TOTAL_COLUMNS = (
    "date",
    "enviro_ref_rr_no",
    "company_name",
    "description",
    "uom",
    "category",
    "remarks",
)

COMPANY_FORM_SUBCELLS = (
    "company_form_label",
    "company_form_value",
)


@dataclass(frozen=True)
class ExtractedTableRow:
    """One physical row from the ruled RR table."""

    page_number: int
    physical_row_number: int
    row_type: str
    bbox_points: dict[str, float]
    cells: dict[str, str | None]
    company_form_subcells: dict[str, str | None] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class PageTableSummary:
    """Auditable geometry and row classifications for one table page."""

    page_number: int
    page_role: str
    table_bbox_points: dict[str, float]
    column_boundaries_points: list[float]
    row_boundaries_points: list[float]
    physical_row_count: int
    company_subcolumn_boundary_points: float | None = None
    header_row_numbers: list[int] = field(default_factory=list)
    detail_row_numbers: list[int] = field(default_factory=list)
    total_row_numbers: list[int] = field(default_factory=list)
    blank_row_numbers: list[int] = field(default_factory=list)
    auxiliary_row_numbers: list[int] = field(default_factory=list)


def find_row_boundaries(
    page: Any,
    table: TableLayout,
) -> list[float]:
    """Find full-width horizontal table rules and cluster duplicate strokes."""

    if table.bbox_points is None:
        return []

    table_x0 = float(table.bbox_points["x0"])
    table_x1 = float(table.bbox_points["x1"])
    table_top = float(table.bbox_points["top"])
    table_bottom = float(table.bbox_points["bottom"])
    positions: list[float] = []

    for line in page.lines:
        line_top = float(line["top"])
        line_bottom = float(line["bottom"])
        if abs(line_bottom - line_top) > HORIZONTAL_LINE_TOLERANCE:
            continue
        if not (
            table_top - TABLE_EDGE_TOLERANCE
            <= line_top
            <= table_bottom + TABLE_EDGE_TOLERANCE
        ):
            continue
        if float(line["x0"]) > table_x0 + TABLE_EDGE_TOLERANCE:
            continue
        if float(line["x1"]) < table_x1 - TABLE_EDGE_TOLERANCE:
            continue
        positions.append((line_top + line_bottom) / 2.0)

    return [
        round(position, 3)
        for position in cluster_positions(
            positions,
            ROW_BOUNDARY_CLUSTER_TOLERANCE,
        )
    ]


def find_company_subcolumn_boundary(
    page: Any,
    table: TableLayout,
) -> float:
    """Find the ruled divider hidden beneath the merged COMPANY NAME header.

    The printed COMPANY NAME heading spans two underlying form cells. On the
    metadata rows, the left subcell contains a label and the right subcell
    contains its value. The divider is detected from vertical vector rules
    inside that recognized high-level column.
    """

    if (
        table.bbox_points is None
        or len(table.column_boundaries_points) != len(TABLE_COLUMNS) + 1
    ):
        raise ValueError("Unsupported table geometry for company subcolumns.")

    company_left = float(table.column_boundaries_points[2])
    company_right = float(table.column_boundaries_points[3])
    table_top = float(table.bbox_points["top"])
    table_bottom = float(table.bbox_points["bottom"])
    table_height = table_bottom - table_top

    segments: list[tuple[float, float]] = []
    for line in page.lines:
        x0 = float(line["x0"])
        x1 = float(line["x1"])
        if abs(x1 - x0) > VERTICAL_LINE_TOLERANCE:
            continue
        position = (x0 + x1) / 2.0
        if not (
            company_left + TABLE_EDGE_TOLERANCE
            < position
            < company_right - TABLE_EDGE_TOLERANCE
        ):
            continue

        overlap_top = max(table_top, float(line["top"]))
        overlap_bottom = min(table_bottom, float(line["bottom"]))
        overlap = max(0.0, overlap_bottom - overlap_top)
        if overlap > 0:
            segments.append((position, overlap))

    clustered_positions = cluster_positions(
        (position for position, _ in segments),
        COLUMN_BOUNDARY_CLUSTER_TOLERANCE,
    )
    minimum_overlap = max(
        MINIMUM_SUBCOLUMN_OVERLAP_POINTS,
        table_height * MINIMUM_SUBCOLUMN_OVERLAP_RATIO,
    )
    qualified = []
    for position in clustered_positions:
        total_overlap = sum(
            overlap
            for segment_position, overlap in segments
            if abs(segment_position - position)
            <= COLUMN_BOUNDARY_CLUSTER_TOLERANCE
        )
        if total_overlap >= minimum_overlap:
            qualified.append(position)

    if len(qualified) != 1:
        raise ValueError(
            "Expected exactly one COMPANY NAME subcolumn divider; "
            f"found {len(qualified)}."
        )
    return round(float(qualified[0]), 3)


def word_center(word: dict[str, Any]) -> tuple[float, float]:
    """Return the geometric center of a pdfplumber word."""

    return (
        (float(word["x0"]) + float(word["x1"])) / 2.0,
        (float(word["top"]) + float(word["bottom"])) / 2.0,
    )


def words_in_cell(
    words: list[dict[str, Any]],
    *,
    left: float,
    right: float,
    top: float,
    bottom: float,
) -> list[dict[str, Any]]:
    """Assign words to a physical cell using each word's center point."""

    selected = []
    for word in words:
        center_x, center_y = word_center(word)
        if not left <= center_x < right:
            continue
        if not top <= center_y < bottom:
            continue
        selected.append(word)

    return sorted(
        selected,
        key=lambda word: (
            round(float(word["top"]) / 2.0) * 2.0,
            float(word["x0"]),
        ),
    )


def cell_text(words: list[dict[str, Any]]) -> str | None:
    """Join cell words in stable reading order, preserving source spelling."""

    if not words:
        return None
    value = " ".join(str(word["text"]).strip() for word in words).strip()
    return value or None


def classify_table_row(cells: dict[str, str | None]) -> str:
    """Classify a physical row using exact, explicit cell rules."""

    description_tokens = normalize_tokens(cells.get("description") or "")
    date_tokens = normalize_tokens(cells.get("date") or "")
    if (
        description_tokens == ["DESCRIPTION"]
        and date_tokens == ["DATE"]
    ):
        return HEADER_ROW
    if cells.get("description"):
        return DETAIL_ROW
    if (
        all(cells.get(column) for column in REQUIRED_TOTAL_COLUMNS)
        and not any(cells.get(column) for column in NON_TOTAL_COLUMNS)
    ):
        return TOTAL_ROW
    if any(value for value in cells.values()):
        return AUXILIARY_ROW
    return BLANK_ROW


def extract_page_table(
    page: Any,
    page_layout: PageLayoutResult,
) -> tuple[list[ExtractedTableRow], PageTableSummary]:
    """Extract and classify every ruled physical row on one RR page."""

    table = page_layout.table
    if (
        table is None
        or not table.found
        or table.bbox_points is None
        or len(table.column_boundaries_points) != len(TABLE_COLUMNS) + 1
    ):
        raise ValueError(
            f"Page {page_layout.page_number} has no supported table geometry."
        )

    row_boundaries = find_row_boundaries(page, table)
    if len(row_boundaries) < 2:
        raise ValueError(
            f"Page {page_layout.page_number} has fewer than two row boundaries."
        )

    company_split = find_company_subcolumn_boundary(page, table)
    words = page.extract_words(
        keep_blank_chars=False,
        use_text_flow=False,
    )
    rows: list[ExtractedTableRow] = []
    for physical_row_number, (row_top, row_bottom) in enumerate(
        zip(row_boundaries, row_boundaries[1:]),
        start=1,
    ):
        cells: dict[str, str | None] = {}
        for column_name, left, right in zip(
            TABLE_COLUMNS,
            table.column_boundaries_points,
            table.column_boundaries_points[1:],
        ):
            cells[column_name] = cell_text(
                words_in_cell(
                    words,
                    left=float(left),
                    right=float(right),
                    top=float(row_top),
                    bottom=float(row_bottom),
                )
            )

        company_form_subcells = {
            subcell_name: cell_text(
                words_in_cell(
                    words,
                    left=float(left),
                    right=float(right),
                    top=float(row_top),
                    bottom=float(row_bottom),
                )
            )
            for subcell_name, left, right in (
                (
                    COMPANY_FORM_SUBCELLS[0],
                    table.column_boundaries_points[2],
                    company_split,
                ),
                (
                    COMPANY_FORM_SUBCELLS[1],
                    company_split,
                    table.column_boundaries_points[3],
                ),
            )
        }
        rows.append(
            ExtractedTableRow(
                page_number=page_layout.page_number,
                physical_row_number=physical_row_number,
                row_type=classify_table_row(cells),
                bbox_points={
                    "x0": round(float(table.bbox_points["x0"]), 3),
                    "top": round(float(row_top), 3),
                    "x1": round(float(table.bbox_points["x1"]), 3),
                    "bottom": round(float(row_bottom), 3),
                },
                cells=cells,
                company_form_subcells=company_form_subcells,
            )
        )

    rows_by_type = {
        row_type: [
            row.physical_row_number
            for row in rows
            if row.row_type == row_type
        ]
        for row_type in (
            HEADER_ROW,
            DETAIL_ROW,
            TOTAL_ROW,
            BLANK_ROW,
            AUXILIARY_ROW,
        )
    }
    summary = PageTableSummary(
        page_number=page_layout.page_number,
        page_role=page_layout.page_role,
        table_bbox_points=dict(table.bbox_points),
        column_boundaries_points=list(table.column_boundaries_points),
        row_boundaries_points=row_boundaries,
        physical_row_count=len(rows),
        company_subcolumn_boundary_points=company_split,
        header_row_numbers=rows_by_type[HEADER_ROW],
        detail_row_numbers=rows_by_type[DETAIL_ROW],
        total_row_numbers=rows_by_type[TOTAL_ROW],
        blank_row_numbers=rows_by_type[BLANK_ROW],
        auxiliary_row_numbers=rows_by_type[AUXILIARY_ROW],
    )
    return rows, summary
