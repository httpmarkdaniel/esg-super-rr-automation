"""Deterministic template and layout recognition for RR PDFs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pdfplumber

from rr_pdf_consolidator.preflight import (
    BLANK_PAGE,
    NATIVE_TEXT_PAGE,
    PREFLIGHT_PASS_STATUSES,
    SPARSE_TEXT_PAGE,
    PreflightResult,
    inspect_pdf,
)


TEMPLATE_ID = "HEPI_RR_WHSE4003_REV02"
MULTIPAGE_TEMPLATE_ID = "HEPI_RR_WHSE4003_REV02_MULTIPAGE"
INPUT_CONTRACT = "ONE_RR_PER_PDF"
EXPECTED_TABLE_COLUMNS = 11
VERTICAL_LINE_TOLERANCE = 0.5
HORIZONTAL_LINE_TOLERANCE = 0.5
SPAN_CLUSTER_TOLERANCE = 2.0
POSITION_CLUSTER_TOLERANCE = 1.0
MINIMUM_TABLE_HEIGHT_RATIO = 0.10
MINIMUM_TABLE_WIDTH_RATIO = 0.75
TABLE_HEADER_STRIP_POINTS = 65.0
CONTINUATION_SIGNATURE_TOLERANCE = 0.05
CONTINUATION_BOUNDARY_POSITION_TOLERANCE = 0.01
MAXIMUM_PHRASE_VERTICAL_SPREAD = 6.0
COMPANY_NAME_COLUMN_INDEX = 2
FOOTER_OVERFLOW_LINE_TOLERANCE = 2.0
FOOTER_OVERFLOW_MAXIMUM_TOKEN_COUNT = 40
FOOTER_OVERFLOW_REGION = (0.0, 0.0, 0.55, 0.25)
FOOTER_OVERFLOW_LINE_PREFIXES = (
    (("PREPARED", "BY"), ("REPORT", "BY")),
    (("WAREHOUSE", "WORKER"),),
    (("DRIVER", "S", "NAME"), ("DRIVERS", "NAME")),
    (("HEPI", "FORM", "WHSE", "4003", "REV", "02"),),
    (("EFFECTIVITY", "DATE"), ("EFFECTIVE", "DATE")),
)

RR_FORM_PAGE = "RR_FORM_PAGE"
RR_CONTINUATION_PAGE = "RR_CONTINUATION_PAGE"
RR_FOOTER_OVERFLOW_PAGE = "RR_FOOTER_OVERFLOW_PAGE"
UNRECOGNIZED_LAYOUT_PAGE = "UNRECOGNIZED_LAYOUT_PAGE"
BLANK_LAYOUT_PAGE = "BLANK_PAGE"


@dataclass(frozen=True)
class NormalizedRegion:
    """A rectangle expressed as fractions of page width and height."""

    x0: float
    top: float
    x1: float
    bottom: float


@dataclass(frozen=True)
class AnchorRule:
    """One exact phrase expected in a normalized page region."""

    name: str
    phrase: str
    region: NormalizedRegion
    required: bool = True


@dataclass(frozen=True)
class TokenItem:
    """One normalized token with its source word geometry."""

    token: str
    raw_text: str
    x0: float
    top: float
    x1: float
    bottom: float


@dataclass(frozen=True)
class AnchorMatch:
    """Result of applying one anchor rule."""

    name: str
    phrase: str
    required: bool
    found: bool
    matched_text: str | None = None
    bbox_points: dict[str, float] | None = None
    bbox_normalized: dict[str, float] | None = None


@dataclass(frozen=True)
class TableLayout:
    """Detected RR table geometry and header checks."""

    found: bool
    column_count: int = 0
    bbox_points: dict[str, float] | None = None
    bbox_normalized: dict[str, float] | None = None
    column_boundaries_points: list[float] = field(default_factory=list)
    column_boundaries_normalized: list[float] = field(default_factory=list)
    column_width_signature: list[float] = field(default_factory=list)
    horizontal_line_count: int = 0
    header_matches: dict[str, bool] = field(default_factory=dict)
    headers_passed: bool = False
    detection_rule: str = "NOT_FOUND"


@dataclass
class PageLayoutResult:
    """Layout recognition result for one page."""

    page_number: int
    preflight_page_type: str
    page_role: str
    status: str
    anchors: list[AnchorMatch] = field(default_factory=list)
    required_anchors_passed: bool = False
    header_sequence_valid: bool = False
    missing_required_anchors: list[str] = field(default_factory=list)
    table: TableLayout | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class LayoutResult:
    """Document-level layout recognition result."""

    filename: str
    source_path: str
    sha256: str
    preflight_status: str
    input_contract: str = INPUT_CONTRACT
    template_id: str | None = None
    status: str = "PREFLIGHT_FAILED"
    pages: list[PageLayoutResult] = field(default_factory=list)
    preflight_warnings: list[str] = field(default_factory=list)
    contract_violations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


CORE_ANCHOR_RULES = (
    AnchorRule(
        "company_title",
        "ENVIROCYCLE PHILIPPINES INC",
        NormalizedRegion(0.00, 0.00, 0.60, 0.12),
    ),
    AnchorRule(
        "report_title",
        "INCOMING RECEIVING REPORT",
        NormalizedRegion(0.40, 0.00, 1.00, 0.12),
    ),
    AnchorRule(
        "order_type_label",
        "ORDER TYPE",
        NormalizedRegion(0.45, 0.02, 0.92, 0.25),
    ),
    AnchorRule(
        "reference_no_label",
        "REFERENCE NO",
        NormalizedRegion(0.45, 0.02, 0.92, 0.25),
    ),
    AnchorRule(
        "received_date_label",
        "RECEIVED DATE",
        NormalizedRegion(0.45, 0.02, 0.92, 0.25),
    ),
    AnchorRule(
        "job_no_label",
        "JOB NO",
        NormalizedRegion(0.45, 0.02, 0.92, 0.25),
    ),
    AnchorRule(
        "schedule_date_label",
        "SCHEDULE DATE",
        NormalizedRegion(0.45, 0.02, 0.92, 0.25),
    ),
    AnchorRule(
        "account_rep_label",
        "ACCOUNT REP",
        NormalizedRegion(0.45, 0.02, 0.92, 0.25),
    ),
    AnchorRule(
        "table_title",
        "INCOMING RECEIVING REPORT",
        NormalizedRegion(0.00, 0.20, 0.60, 0.55),
    ),
    AnchorRule(
        "form_revision",
        "HEPI FORM WHSE 4003 REV 02",
        NormalizedRegion(0.00, 0.78, 0.60, 1.00),
        required=False,
    ),
)

HEADER_SEQUENCE = (
    "order_type_label",
    "reference_no_label",
    "received_date_label",
    "job_no_label",
    "schedule_date_label",
    "account_rep_label",
)

TABLE_HEADER_PHRASES = {
    "date": "DATE",
    "company_name": "COMPANY NAME",
    "description": "DESCRIPTION",
    "kilos": "KILOS",
    "uom": "UOM",
    "category": "CATEGORY",
    "remarks": "REMARKS",
}

# Some PDF generators omit the visual space between two adjacent header cells
# from the text layer. These exact, known combinations are accepted without
# fuzzy or substring matching.
ALLOWED_MERGED_TABLE_HEADERS = (
    ("category", "remarks"),
)


def normalize_tokens(text: str) -> list[str]:
    """Convert text to deterministic uppercase alphanumeric tokens."""

    return re.findall(r"[A-Z0-9]+", text.upper())


def extract_tokens(page: Any) -> list[TokenItem]:
    """Extract normalized word tokens in stable reading order."""

    words = page.extract_words(
        keep_blank_chars=False,
        use_text_flow=False,
    )
    words = sorted(
        words,
        key=lambda word: (
            round(float(word["top"]) / 2.0) * 2.0,
            float(word["x0"]),
        ),
    )

    tokens: list[TokenItem] = []
    for word in words:
        for token in normalize_tokens(str(word["text"])):
            tokens.append(
                TokenItem(
                    token=token,
                    raw_text=str(word["text"]),
                    x0=float(word["x0"]),
                    top=float(word["top"]),
                    x1=float(word["x1"]),
                    bottom=float(word["bottom"]),
                )
            )
    return tokens


def normalized_region_to_points(
    region: NormalizedRegion,
    page_width: float,
    page_height: float,
) -> tuple[float, float, float, float]:
    """Convert a normalized region to PDF point coordinates."""

    return (
        region.x0 * page_width,
        region.top * page_height,
        region.x1 * page_width,
        region.bottom * page_height,
    )


def token_in_region(
    token: TokenItem,
    region_points: tuple[float, float, float, float],
) -> bool:
    """Return True when a token center lies inside a region."""

    x0, top, x1, bottom = region_points
    center_x = (token.x0 + token.x1) / 2.0
    center_y = (token.top + token.bottom) / 2.0
    return x0 <= center_x <= x1 and top <= center_y <= bottom


def matches_footer_overflow_page(
    tokens: list[TokenItem],
    page_width: float,
    page_height: float,
) -> bool:
    """Recognize an exact five-line RR footer on a trailing page."""

    if (
        not tokens
        or len(tokens) > FOOTER_OVERFLOW_MAXIMUM_TOKEN_COUNT
    ):
        return False
    region_points = normalized_region_to_points(
        NormalizedRegion(*FOOTER_OVERFLOW_REGION),
        page_width,
        page_height,
    )
    if any(
        not token_in_region(token, region_points) for token in tokens
    ):
        return False

    ordered_tokens = sorted(
        tokens,
        key=lambda token: (token.top, token.x0),
    )
    lines: list[list[TokenItem]] = []
    line_tops: list[float] = []
    for token in ordered_tokens:
        if (
            not lines
            or abs(token.top - line_tops[-1])
            > FOOTER_OVERFLOW_LINE_TOLERANCE
        ):
            lines.append([token])
            line_tops.append(token.top)
        else:
            lines[-1].append(token)
    if len(lines) != len(FOOTER_OVERFLOW_LINE_PREFIXES):
        return False

    for line, allowed_prefixes in zip(
        lines,
        FOOTER_OVERFLOW_LINE_PREFIXES,
    ):
        line_values = [
            token.token for token in sorted(line, key=lambda item: item.x0)
        ]
        if not any(
            line_values[: len(prefix)] == list(prefix)
            for prefix in allowed_prefixes
        ):
            return False
    return True


def find_phrase(
    tokens: list[TokenItem],
    phrase: str,
    region_points: tuple[float, float, float, float],
    *,
    maximum_vertical_spread: float = MAXIMUM_PHRASE_VERTICAL_SPREAD,
) -> tuple[TokenItem, ...] | None:
    """Find an exact normalized token sequence inside an absolute region."""

    phrase_tokens = normalize_tokens(phrase)
    if not phrase_tokens:
        return None

    region_tokens = [
        token for token in tokens if token_in_region(token, region_points)
    ]
    phrase_length = len(phrase_tokens)
    for start in range(len(region_tokens) - phrase_length + 1):
        candidate = region_tokens[start : start + phrase_length]
        if [token.token for token in candidate] != phrase_tokens:
            continue
        tops = [token.top for token in candidate]
        bottoms = [token.bottom for token in candidate]
        if max(bottoms) - min(tops) > maximum_vertical_spread * 2.0:
            continue
        return tuple(candidate)
    return None


def detect_table_header_matches(
    tokens: list[TokenItem],
    header_region: tuple[float, float, float, float],
) -> dict[str, bool]:
    """Match expected headers, including explicitly allowed merged cell text."""

    matches = {
        name: find_phrase(tokens, phrase, header_region) is not None
        for name, phrase in TABLE_HEADER_PHRASES.items()
    }
    region_token_values = {
        token.token for token in tokens if token_in_region(token, header_region)
    }

    for header_names in ALLOWED_MERGED_TABLE_HEADERS:
        merged_value = "".join(
            normalize_tokens(TABLE_HEADER_PHRASES[name])[0]
            for name in header_names
        )
        if merged_value in region_token_values:
            for name in header_names:
                matches[name] = True

    return matches


def bbox_for_tokens(
    tokens: Iterable[TokenItem],
    page_width: float,
    page_height: float,
) -> tuple[dict[str, float], dict[str, float]]:
    """Return point and normalized bounding boxes for token geometry."""

    token_list = list(tokens)
    x0 = min(token.x0 for token in token_list)
    top = min(token.top for token in token_list)
    x1 = max(token.x1 for token in token_list)
    bottom = max(token.bottom for token in token_list)
    points = {
        "x0": round(x0, 3),
        "top": round(top, 3),
        "x1": round(x1, 3),
        "bottom": round(bottom, 3),
    }
    normalized = {
        "x0": round(x0 / page_width, 6),
        "top": round(top / page_height, 6),
        "x1": round(x1 / page_width, 6),
        "bottom": round(bottom / page_height, 6),
    }
    return points, normalized


def match_anchor(
    tokens: list[TokenItem],
    rule: AnchorRule,
    page_width: float,
    page_height: float,
) -> AnchorMatch:
    """Apply one anchor rule to a page."""

    region_points = normalized_region_to_points(
        rule.region,
        page_width,
        page_height,
    )
    matched_tokens = find_phrase(tokens, rule.phrase, region_points)
    if matched_tokens is None:
        return AnchorMatch(
            name=rule.name,
            phrase=rule.phrase,
            required=rule.required,
            found=False,
        )

    points, normalized = bbox_for_tokens(
        matched_tokens,
        page_width,
        page_height,
    )
    return AnchorMatch(
        name=rule.name,
        phrase=rule.phrase,
        required=rule.required,
        found=True,
        matched_text=" ".join(token.token for token in matched_tokens),
        bbox_points=points,
        bbox_normalized=normalized,
    )


def validate_header_sequence(anchor_matches: list[AnchorMatch]) -> bool:
    """Require the right-side header labels to appear in their known order."""

    by_name = {match.name: match for match in anchor_matches}
    sequence_matches = [by_name.get(name) for name in HEADER_SEQUENCE]
    if any(match is None or not match.found for match in sequence_matches):
        return False

    tops = [
        float(match.bbox_points["top"])
        for match in sequence_matches
        if match is not None and match.bbox_points is not None
    ]
    return all(first < second for first, second in zip(tops, tops[1:]))


def cluster_positions(values: Iterable[float], tolerance: float) -> list[float]:
    """Cluster nearly equal coordinates and return stable means."""

    clusters: list[list[float]] = []
    for value in sorted(values):
        for cluster in clusters:
            mean = sum(cluster) / len(cluster)
            if abs(value - mean) <= tolerance:
                cluster.append(value)
                break
        else:
            clusters.append([value])
    return [sum(cluster) / len(cluster) for cluster in clusters]


def normalized_bbox(
    x0: float,
    top: float,
    x1: float,
    bottom: float,
    page_width: float,
    page_height: float,
) -> dict[str, float]:
    """Normalize a point bounding box against page dimensions."""

    return {
        "x0": round(x0 / page_width, 6),
        "top": round(top / page_height, 6),
        "x1": round(x1 / page_width, 6),
        "bottom": round(bottom / page_height, 6),
    }


def detect_table_layout(
    page: Any,
    tokens: list[TokenItem],
) -> TableLayout:
    """Detect the ruled 11-column RR table from actual vector lines."""

    page_width = float(page.width)
    page_height = float(page.height)
    minimum_height = page_height * MINIMUM_TABLE_HEIGHT_RATIO

    span_clusters: list[dict[str, Any]] = []
    for line in page.lines:
        x0 = float(line["x0"])
        x1 = float(line["x1"])
        top = float(line["top"])
        bottom = float(line["bottom"])
        if abs(x1 - x0) > VERTICAL_LINE_TOLERANCE:
            continue
        if bottom - top < minimum_height:
            continue

        for cluster in span_clusters:
            if (
                abs(top - cluster["top_mean"]) <= SPAN_CLUSTER_TOLERANCE
                and abs(bottom - cluster["bottom_mean"])
                <= SPAN_CLUSTER_TOLERANCE
            ):
                cluster["tops"].append(top)
                cluster["bottoms"].append(bottom)
                cluster["xs"].append((x0 + x1) / 2.0)
                cluster["top_mean"] = sum(cluster["tops"]) / len(cluster["tops"])
                cluster["bottom_mean"] = sum(cluster["bottoms"]) / len(
                    cluster["bottoms"]
                )
                break
        else:
            span_clusters.append(
                {
                    "tops": [top],
                    "bottoms": [bottom],
                    "xs": [(x0 + x1) / 2.0],
                    "top_mean": top,
                    "bottom_mean": bottom,
                }
            )

    candidates: list[
        tuple[int, float, float, list[float], dict[str, Any]]
    ] = []
    for cluster in span_clusters:
        xs = cluster_positions(cluster["xs"], POSITION_CLUSTER_TOLERANCE)
        if len(xs) != EXPECTED_TABLE_COLUMNS + 1:
            continue

        x0 = min(xs)
        x1 = max(xs)
        top = float(cluster["top_mean"])
        bottom = float(cluster["bottom_mean"])
        width_ratio = (x1 - x0) / page_width
        if width_ratio < MINIMUM_TABLE_WIDTH_RATIO:
            continue

        horizontal_count = 0
        for line in page.lines:
            line_x0 = float(line["x0"])
            line_x1 = float(line["x1"])
            line_top = float(line["top"])
            line_bottom = float(line["bottom"])
            if abs(line_bottom - line_top) > HORIZONTAL_LINE_TOLERANCE:
                continue
            if not top - 2.0 <= line_top <= bottom + 2.0:
                continue
            if line_x0 > x0 + 2.0 or line_x1 < x1 - 2.0:
                continue
            horizontal_count += 1

        candidates.append(
            (
                horizontal_count,
                bottom - top,
                width_ratio,
                xs,
                cluster,
            )
        )

    if not candidates:
        # Some Excel-generated form pages draw the header-row dividers as
        # short segments, then continue the same boundaries below the header
        # with slightly different starting positions. Recover that exact
        # structure from the shared baseline of five unambiguous headers.
        baseline_tokens = {
            "DESCRIPTION",
            "KILOS",
            "UOM",
            "CATEGORY",
            "REMARKS",
        }
        for description_token in (
            token for token in tokens if token.token == "DESCRIPTION"
        ):
            description_center = (
                description_token.top + description_token.bottom
            ) / 2.0
            same_baseline = {
                token.token
                for token in tokens
                if token.token in baseline_tokens
                and abs(
                    (
                        (token.top + token.bottom) / 2.0
                    )
                    - description_center
                )
                <= MAXIMUM_PHRASE_VERTICAL_SPREAD
            }
            if same_baseline != baseline_tokens:
                continue

            crossing_segments = []
            for line in page.lines:
                x0 = float(line["x0"])
                x1 = float(line["x1"])
                top = float(line["top"])
                bottom = float(line["bottom"])
                if abs(x1 - x0) > VERTICAL_LINE_TOLERANCE:
                    continue
                if top - 1.0 <= description_center <= bottom + 1.0:
                    crossing_segments.append(
                        ((x0 + x1) / 2.0, top, bottom)
                    )
            xs = cluster_positions(
                (segment[0] for segment in crossing_segments),
                POSITION_CLUSTER_TOLERANCE,
            )
            if len(xs) != EXPECTED_TABLE_COLUMNS + 1:
                continue

            x0 = min(xs)
            x1 = max(xs)
            width_ratio = (x1 - x0) / page_width
            if width_ratio < MINIMUM_TABLE_WIDTH_RATIO:
                continue
            top = min(segment[1] for segment in crossing_segments)
            boundary_segments = [
                line
                for line in page.lines
                if abs(float(line["x1"]) - float(line["x0"]))
                <= VERTICAL_LINE_TOLERANCE
                and any(
                    abs(
                        (
                            (
                                float(line["x0"])
                                + float(line["x1"])
                            )
                            / 2.0
                        )
                        - boundary
                    )
                    <= POSITION_CLUSTER_TOLERANCE
                    for boundary in xs
                )
                and float(line["bottom"]) >= description_center
            ]
            bottom = max(
                float(line["bottom"]) for line in boundary_segments
            )
            horizontal_count = 0
            for line in page.lines:
                line_x0 = float(line["x0"])
                line_x1 = float(line["x1"])
                line_top = float(line["top"])
                line_bottom = float(line["bottom"])
                if (
                    abs(line_bottom - line_top)
                    <= HORIZONTAL_LINE_TOLERANCE
                    and top - 2.0 <= line_top <= bottom + 2.0
                    and line_x0 <= x0 + 2.0
                    and line_x1 >= x1 - 2.0
                ):
                    horizontal_count += 1
            candidates.append(
                (
                    horizontal_count,
                    bottom - top,
                    width_ratio,
                    xs,
                    {
                        "top_mean": top,
                        "bottom_mean": bottom,
                    },
                )
            )
            break

    if not candidates:
        return TableLayout(found=False)

    selected = max(candidates, key=lambda item: (item[0], item[1], item[2]))
    selected_xs = selected[3]
    selected_cluster = selected[4]

    x0 = min(selected_xs)
    x1 = max(selected_xs)
    top = float(selected_cluster["top_mean"])
    bottom = float(selected_cluster["bottom_mean"])
    table_width = x1 - x0
    boundary_normalized = [
        round(value / page_width, 6) for value in selected_xs
    ]
    width_signature = [
        round((value - x0) / table_width, 6) for value in selected_xs
    ]

    header_region = (
        x0,
        top,
        x1,
        min(bottom, top + TABLE_HEADER_STRIP_POINTS),
    )
    header_matches = detect_table_header_matches(tokens, header_region)

    return TableLayout(
        found=True,
        column_count=len(selected_xs) - 1,
        bbox_points={
            "x0": round(x0, 3),
            "top": round(top, 3),
            "x1": round(x1, 3),
            "bottom": round(bottom, 3),
        },
        bbox_normalized=normalized_bbox(
            x0,
            top,
            x1,
            bottom,
            page_width,
            page_height,
        ),
        column_boundaries_points=[
            round(value, 3) for value in selected_xs
        ],
        column_boundaries_normalized=boundary_normalized,
        column_width_signature=width_signature,
        horizontal_line_count=selected[0],
        header_matches=header_matches,
        headers_passed=all(header_matches.values()),
        detection_rule="EXACT_11_COLUMN_GRID",
    )


def match_reference_boundaries_with_company_subdivider(
    candidate_boundaries_points: list[float],
    *,
    page_width: float,
    reference_boundaries_normalized: list[float],
    tolerance: float = CONTINUATION_BOUNDARY_POSITION_TOLERANCE,
) -> list[float] | None:
    """Match 11 logical columns while excluding one known form divider."""

    expected_boundary_count = EXPECTED_TABLE_COLUMNS + 1
    if len(reference_boundaries_normalized) != expected_boundary_count:
        return None
    if len(candidate_boundaries_points) != expected_boundary_count + 1:
        return None

    candidate_normalized = [
        value / page_width for value in candidate_boundaries_points
    ]
    unused_indexes = set(range(len(candidate_normalized)))
    selected_indexes: list[int] = []
    for reference in reference_boundaries_normalized:
        nearest_index = min(
            unused_indexes,
            key=lambda index: abs(
                candidate_normalized[index] - reference
            ),
        )
        if (
            abs(candidate_normalized[nearest_index] - reference)
            > tolerance
        ):
            return None
        selected_indexes.append(nearest_index)
        unused_indexes.remove(nearest_index)

    if len(unused_indexes) != 1:
        return None
    extra_index = next(iter(unused_indexes))
    extra_boundary = candidate_normalized[extra_index]
    company_left = reference_boundaries_normalized[
        COMPANY_NAME_COLUMN_INDEX
    ]
    company_right = reference_boundaries_normalized[
        COMPANY_NAME_COLUMN_INDEX + 1
    ]
    if not (
        company_left + tolerance
        < extra_boundary
        < company_right - tolerance
    ):
        return None

    return [
        candidate_boundaries_points[index]
        for index in selected_indexes
    ]


def detect_continuation_table_with_company_subdivider(
    page: Any,
    reference_boundaries_normalized: list[float],
) -> TableLayout:
    """Detect a headerless continuation with one full-height form divider."""

    page_width = float(page.width)
    page_height = float(page.height)
    minimum_height = page_height * MINIMUM_TABLE_HEIGHT_RATIO
    span_clusters: list[dict[str, Any]] = []

    for line in page.lines:
        x0 = float(line["x0"])
        x1 = float(line["x1"])
        top = float(line["top"])
        bottom = float(line["bottom"])
        if abs(x1 - x0) > VERTICAL_LINE_TOLERANCE:
            continue
        if bottom - top < minimum_height:
            continue

        for cluster in span_clusters:
            if (
                abs(top - cluster["top_mean"])
                <= SPAN_CLUSTER_TOLERANCE
                and abs(bottom - cluster["bottom_mean"])
                <= SPAN_CLUSTER_TOLERANCE
            ):
                cluster["tops"].append(top)
                cluster["bottoms"].append(bottom)
                cluster["xs"].append((x0 + x1) / 2.0)
                cluster["top_mean"] = (
                    sum(cluster["tops"]) / len(cluster["tops"])
                )
                cluster["bottom_mean"] = (
                    sum(cluster["bottoms"]) / len(cluster["bottoms"])
                )
                break
        else:
            span_clusters.append(
                {
                    "tops": [top],
                    "bottoms": [bottom],
                    "xs": [(x0 + x1) / 2.0],
                    "top_mean": top,
                    "bottom_mean": bottom,
                }
            )

    candidates: list[
        tuple[int, float, float, list[float], dict[str, Any]]
    ] = []
    for cluster in span_clusters:
        candidate_xs = cluster_positions(
            cluster["xs"],
            POSITION_CLUSTER_TOLERANCE,
        )
        selected_xs = match_reference_boundaries_with_company_subdivider(
            candidate_xs,
            page_width=page_width,
            reference_boundaries_normalized=(
                reference_boundaries_normalized
            ),
        )
        if selected_xs is None:
            continue

        x0 = min(selected_xs)
        x1 = max(selected_xs)
        top = float(cluster["top_mean"])
        bottom = float(cluster["bottom_mean"])
        width_ratio = (x1 - x0) / page_width
        if width_ratio < MINIMUM_TABLE_WIDTH_RATIO:
            continue

        horizontal_count = 0
        for line in page.lines:
            line_x0 = float(line["x0"])
            line_x1 = float(line["x1"])
            line_top = float(line["top"])
            line_bottom = float(line["bottom"])
            if (
                abs(line_bottom - line_top)
                > HORIZONTAL_LINE_TOLERANCE
            ):
                continue
            if not top - 2.0 <= line_top <= bottom + 2.0:
                continue
            if line_x0 > x0 + 2.0 or line_x1 < x1 - 2.0:
                continue
            horizontal_count += 1

        candidates.append(
            (
                horizontal_count,
                bottom - top,
                width_ratio,
                selected_xs,
                cluster,
            )
        )

    if not candidates:
        return TableLayout(found=False)

    selected = max(candidates, key=lambda item: (item[0], item[1], item[2]))
    selected_xs = selected[3]
    selected_cluster = selected[4]
    x0 = min(selected_xs)
    x1 = max(selected_xs)
    top = float(selected_cluster["top_mean"])
    bottom = float(selected_cluster["bottom_mean"])
    table_width = x1 - x0

    return TableLayout(
        found=True,
        column_count=len(selected_xs) - 1,
        bbox_points={
            "x0": round(x0, 3),
            "top": round(top, 3),
            "x1": round(x1, 3),
            "bottom": round(bottom, 3),
        },
        bbox_normalized=normalized_bbox(
            x0,
            top,
            x1,
            bottom,
            page_width,
            page_height,
        ),
        column_boundaries_points=[
            round(value, 3) for value in selected_xs
        ],
        column_boundaries_normalized=[
            round(value / page_width, 6) for value in selected_xs
        ],
        column_width_signature=[
            round((value - x0) / table_width, 6)
            for value in selected_xs
        ],
        horizontal_line_count=selected[0],
        header_matches={},
        headers_passed=False,
        detection_rule=(
            "REFERENCE_GRID_WITH_COMPANY_NAME_SUBDIVIDER"
        ),
    )


def signatures_compatible(
    first: list[float],
    second: list[float],
    *,
    tolerance: float = CONTINUATION_SIGNATURE_TOLERANCE,
) -> bool:
    """Compare relative table-column boundary signatures."""

    if not first or len(first) != len(second):
        return False
    return all(abs(left - right) <= tolerance for left, right in zip(first, second))


def determine_layout_status(
    page_results: list[PageLayoutResult],
    preflight_status: str,
) -> str:
    """Determine the final document-level layout status."""

    if preflight_status not in PREFLIGHT_PASS_STATUSES:
        return "PREFLIGHT_FAILED"
    recognized_roles = {
        RR_FORM_PAGE,
        RR_CONTINUATION_PAGE,
        RR_FOOTER_OVERFLOW_PAGE,
        BLANK_LAYOUT_PAGE,
    }
    if not page_results:
        return "LAYOUT_MISMATCH"
    if any(page.page_role not in recognized_roles for page in page_results):
        return "LAYOUT_MISMATCH"
    if validate_one_rr_contract(page_results):
        return "LAYOUT_MISMATCH"
    return "LAYOUT_RECOGNIZED"


def validate_one_rr_contract(
    page_results: list[PageLayoutResult],
) -> list[str]:
    """Validate the deterministic page-role rules for one RR per PDF."""

    meaningful_pages = [
        page
        for page in page_results
        if page.page_role != BLANK_LAYOUT_PAGE
    ]
    if not meaningful_pages:
        return ["The PDF contains no nonblank RR form page."]

    violations: list[str] = []
    first_page = meaningful_pages[0]
    if first_page.page_role != RR_FORM_PAGE:
        violations.append(
            "The first nonblank page must be an RR form page; "
            f"page {first_page.page_number} is {first_page.page_role}."
        )

    form_pages = [
        page.page_number
        for page in meaningful_pages
        if page.page_role == RR_FORM_PAGE
    ]
    if len(form_pages) > 1:
        page_list = ", ".join(str(page) for page in form_pages)
        violations.append(
            "The one-RR-per-PDF contract allows one RR form start; "
            f"form pages were found at pages {page_list}."
        )

    footer_pages = [
        page.page_number
        for page in meaningful_pages
        if page.page_role == RR_FOOTER_OVERFLOW_PAGE
    ]
    if len(footer_pages) > 1:
        page_list = ", ".join(str(page) for page in footer_pages)
        violations.append(
            "The one-RR-per-PDF contract allows at most one footer "
            f"overflow page; pages were found at {page_list}."
        )
    if (
        footer_pages
        and meaningful_pages[-1].page_role
        != RR_FOOTER_OVERFLOW_PAGE
    ):
        violations.append(
            "An RR footer overflow page must be the final nonblank "
            "page."
        )

    return violations


def inspect_layout(path: Path) -> LayoutResult:
    """Run preflight, anchors, grid detection, and page-role recognition."""

    preflight: PreflightResult = inspect_pdf(path)
    result = LayoutResult(
        filename=preflight.filename,
        source_path=preflight.source_path,
        sha256=preflight.sha256,
        preflight_status=preflight.status,
        preflight_warnings=list(preflight.warnings),
    )
    if preflight.status not in PREFLIGHT_PASS_STATUSES:
        result.status = "PREFLIGHT_FAILED"
        result.errors.append(
            f"Preflight status is {preflight.status}; layout inspection was not run."
        )
        return result

    preflight_pages = {
        page.page_number: page for page in preflight.pages
    }
    reference_signature: list[float] | None = None
    reference_boundaries_normalized: list[float] | None = None
    recognized_template_id: str | None = None

    try:
        with pdfplumber.open(preflight.source_path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                preflight_page = preflight_pages[page_number]
                if preflight_page.page_type == BLANK_PAGE:
                    result.pages.append(
                        PageLayoutResult(
                            page_number=page_number,
                            preflight_page_type=preflight_page.page_type,
                            page_role=BLANK_LAYOUT_PAGE,
                            status="BLANK_PAGE_SKIPPED",
                        )
                    )
                    continue

                if preflight_page.page_type not in {
                    NATIVE_TEXT_PAGE,
                    SPARSE_TEXT_PAGE,
                }:
                    result.pages.append(
                        PageLayoutResult(
                            page_number=page_number,
                            preflight_page_type=preflight_page.page_type,
                            page_role=UNRECOGNIZED_LAYOUT_PAGE,
                            status="UNSUPPORTED_PREFLIGHT_PAGE_TYPE",
                            errors=[
                                f"Unsupported preflight page type: "
                                f"{preflight_page.page_type}"
                            ],
                        )
                    )
                    continue

                page_width = float(page.width)
                page_height = float(page.height)
                tokens = extract_tokens(page)
                anchor_matches = [
                    match_anchor(
                        tokens,
                        rule,
                        page_width,
                        page_height,
                    )
                    for rule in CORE_ANCHOR_RULES
                ]
                missing_required = [
                    match.name
                    for match in anchor_matches
                    if match.required and not match.found
                ]
                required_passed = not missing_required
                sequence_valid = validate_header_sequence(anchor_matches)
                table = detect_table_layout(page, tokens)
                if (
                    not required_passed
                    and reference_signature is not None
                    and reference_boundaries_normalized is not None
                    and (
                        not table.found
                        or table.column_count
                        != EXPECTED_TABLE_COLUMNS
                        or not signatures_compatible(
                            reference_signature,
                            table.column_width_signature,
                        )
                    )
                ):
                    matched_continuation = (
                        detect_continuation_table_with_company_subdivider(
                            page,
                            reference_boundaries_normalized,
                        )
                    )
                    if matched_continuation.found:
                        table = matched_continuation

                errors: list[str] = []
                page_role = UNRECOGNIZED_LAYOUT_PAGE
                page_status = "LAYOUT_MISMATCH"

                form_layout_passed = (
                    required_passed
                    and sequence_valid
                    and table.found
                    and table.column_count == EXPECTED_TABLE_COLUMNS
                    and table.headers_passed
                )
                multipage_form_layout_passed = (
                    missing_required == ["company_title"]
                    and len(pdf.pages) > 1
                    and sequence_valid
                    and table.found
                    and table.column_count == EXPECTED_TABLE_COLUMNS
                    and table.headers_passed
                )
                if form_layout_passed or multipage_form_layout_passed:
                    page_role = RR_FORM_PAGE
                    page_status = "LAYOUT_RECOGNIZED"
                    recognized_template_id = (
                        TEMPLATE_ID
                        if form_layout_passed
                        else MULTIPAGE_TEMPLATE_ID
                    )
                    if reference_signature is None:
                        reference_signature = table.column_width_signature
                        reference_boundaries_normalized = (
                            table.column_boundaries_normalized
                        )
                elif (
                    not required_passed
                    and table.found
                    and table.column_count == EXPECTED_TABLE_COLUMNS
                    and reference_signature is not None
                    and signatures_compatible(
                        reference_signature,
                        table.column_width_signature,
                    )
                ):
                    page_role = RR_CONTINUATION_PAGE
                    page_status = "CONTINUATION_LAYOUT_RECOGNIZED"
                elif (
                    reference_signature is not None
                    and not table.found
                    and matches_footer_overflow_page(
                        tokens,
                        page_width,
                        page_height,
                    )
                ):
                    page_role = RR_FOOTER_OVERFLOW_PAGE
                    page_status = "FOOTER_OVERFLOW_RECOGNIZED"
                else:
                    if missing_required:
                        errors.append(
                            "Missing required anchors: "
                            + ", ".join(missing_required)
                        )
                    if not sequence_valid:
                        errors.append("Right-side header label order is invalid.")
                    if not table.found:
                        errors.append("Expected ruled table grid was not found.")
                    elif table.column_count != EXPECTED_TABLE_COLUMNS:
                        errors.append(
                            f"Expected {EXPECTED_TABLE_COLUMNS} table columns; "
                            f"found {table.column_count}."
                        )
                    elif not table.headers_passed:
                        missing_headers = [
                            name
                            for name, matched in table.header_matches.items()
                            if not matched
                        ]
                        errors.append(
                            "Missing table headers: "
                            + ", ".join(missing_headers)
                        )
                    elif reference_signature is not None:
                        errors.append(
                            "Table column signature does not match the recognized "
                            "RR layout."
                        )

                page_warnings: list[str] = []
                if (
                    preflight_page.page_type == SPARSE_TEXT_PAGE
                    and page_role
                    in {
                        RR_FORM_PAGE,
                        RR_CONTINUATION_PAGE,
                        RR_FOOTER_OVERFLOW_PAGE,
                    }
                ):
                    page_warnings.append(
                        "Sparse native text was accepted only after "
                        "deterministic layout validation."
                    )
                if (
                    table.detection_rule
                    == "REFERENCE_GRID_WITH_COMPANY_NAME_SUBDIVIDER"
                ):
                    page_warnings.append(
                        "Continuation grid matched the form-page column "
                        "boundaries after excluding the single full-height "
                        "COMPANY NAME sub-divider."
                    )
                if page_role == RR_FOOTER_OVERFLOW_PAGE:
                    page_warnings.append(
                        "An exact trailing RR footer overflow was "
                        "recognized and excluded from table extraction."
                    )

                result.pages.append(
                    PageLayoutResult(
                        page_number=page_number,
                        preflight_page_type=preflight_page.page_type,
                        page_role=page_role,
                        status=page_status,
                        anchors=anchor_matches,
                        required_anchors_passed=required_passed,
                        header_sequence_valid=sequence_valid,
                        missing_required_anchors=missing_required,
                        table=table,
                        warnings=page_warnings,
                        errors=errors,
                    )
                )
    except Exception as error:
        result.status = "LAYOUT_ERROR"
        result.errors.append(f"{type(error).__name__}: {error}")
        return result

    result.contract_violations = validate_one_rr_contract(result.pages)
    result.status = determine_layout_status(
        result.pages,
        result.preflight_status,
    )
    if result.status == "LAYOUT_RECOGNIZED":
        result.template_id = recognized_template_id
    else:
        result.errors.extend(result.contract_violations)
        result.errors.append(
            "One or more nonblank pages did not match the supported RR layout."
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic RR template and layout recognition."
    )
    parser.add_argument("pdf", type=Path, help="Path to the PDF to inspect.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the JSON result. Otherwise prints to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result = inspect_layout(args.pdf)
    except (FileNotFoundError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1

    output_text = json.dumps(result.to_dict(), indent=2, sort_keys=True)
    if args.output:
        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text + "\n", encoding="utf-8")
        print(f"Layout result written to: {output_path}")
    else:
        print(output_text)

    return 0 if result.status == "LAYOUT_RECOGNIZED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
