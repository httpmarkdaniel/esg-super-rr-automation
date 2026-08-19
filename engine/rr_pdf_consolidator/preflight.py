"""Basic deterministic preflight checks for Receiving Report PDFs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader
from pypdf.generic import ContentStream


MINIMUM_TEXT_CHARACTERS = 100
FULL_PAGE_IMAGE_COVERAGE = 0.80
BLANK_PAGE_MAXIMUM_PATH_OPERATORS = 100
BLANK_PAGE_MAXIMUM_DRAWING_OBJECTS = 25

NATIVE_TEXT_PAGE = "NATIVE_TEXT_PAGE"
BLANK_PAGE = "BLANK_PAGE"
SCANNED_IMAGE_PAGE = "SCANNED_IMAGE_PAGE"
VECTOR_ONLY_PAGE = "VECTOR_ONLY_PAGE"
SPARSE_TEXT_PAGE = "SPARSE_TEXT_PAGE"

PREFLIGHT_PASS_STATUSES = frozenset(
    {
        "BASIC_PREFLIGHT_PASSED",
        "BASIC_PREFLIGHT_PASSED_WITH_WARNINGS",
    }
)
LAYOUT_ELIGIBLE_PAGE_TYPES = frozenset(
    {
        NATIVE_TEXT_PAGE,
        SPARSE_TEXT_PAGE,
        BLANK_PAGE,
    }
)
PATH_OPERATORS = frozenset(
    {"m", "l", "c", "v", "y", "re", "S", "s", "f", "F", "f*", "B", "B*"}
)
TEXT_SHOW_OPERATORS = frozenset({"Tj", "TJ", "'", '"'})


@dataclass(frozen=True)
class PageInspection:
    """Observed properties for one PDF page."""

    page_number: int
    width_points: float
    height_points: float
    rotation_degrees: int
    text_character_count: int
    image_count: int
    vector_line_count: int
    rectangle_count: int
    curve_count: int
    font_count: int
    text_show_operator_count: int
    path_operator_count: int
    has_full_page_image: bool
    page_type: str


@dataclass
class PreflightResult:
    """Machine-readable result of the basic preflight gate."""

    filename: str
    source_path: str
    sha256: str
    file_size_bytes: int
    readable_pdf: bool = False
    encrypted: bool | None = None
    page_count: int | None = None
    total_text_character_count: int = 0
    has_text_layer: bool = False
    has_full_page_image: bool = False
    pages: list[PageInspection] = field(default_factory=list)
    page_type_counts: dict[str, int] = field(default_factory=dict)
    blocking_pages: list[int] = field(default_factory=list)
    warning_pages: list[int] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    status: str = "UNREADABLE_PDF"
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return a stable SHA-256 digest without loading the whole file at once."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest().upper()


def image_covers_page(
    image: dict[str, Any],
    page_width: float,
    page_height: float,
) -> bool:
    """Return True when one raster image covers most of a page."""

    page_area = page_width * page_height
    if page_area <= 0:
        return False

    image_width = float(image.get("width") or 0)
    image_height = float(image.get("height") or 0)
    image_area_ratio = (image_width * image_height) / page_area
    width_ratio = image_width / page_width
    height_ratio = image_height / page_height

    return (
        image_area_ratio >= FULL_PAGE_IMAGE_COVERAGE
        and width_ratio >= FULL_PAGE_IMAGE_COVERAGE
        and height_ratio >= FULL_PAGE_IMAGE_COVERAGE
    )


def count_page_operators(
    reader_page: Any,
    reader: PdfReader,
) -> tuple[int, int]:
    """Count text-showing and path operators in one page's content stream."""

    contents = reader_page.get_contents()
    if contents is None:
        return 0, 0

    counts: Counter[str] = Counter()
    content_stream = ContentStream(contents, reader)
    for _, operator in content_stream.operations:
        operator_name = (
            operator.decode("latin1") if isinstance(operator, bytes) else str(operator)
        )
        counts[operator_name] += 1

    text_show_count = sum(counts[name] for name in TEXT_SHOW_OPERATORS)
    path_count = sum(counts[name] for name in PATH_OPERATORS)
    return text_show_count, path_count


def count_page_fonts(reader_page: Any) -> int:
    """Return the number of font resources directly available to a page."""

    resources = reader_page.get("/Resources")
    if resources is None:
        return 0

    resources = resources.get_object()
    fonts = resources.get("/Font")
    if fonts is None:
        return 0

    return len(fonts.get_object())


def classify_page(
    *,
    text_character_count: int,
    image_count: int,
    has_full_page_image: bool,
    path_operator_count: int,
    drawing_object_count: int,
) -> str:
    """Classify one page using explicit, ordered structural rules."""

    if has_full_page_image:
        return SCANNED_IMAGE_PAGE
    if text_character_count >= MINIMUM_TEXT_CHARACTERS:
        return NATIVE_TEXT_PAGE
    if text_character_count > 0:
        return SPARSE_TEXT_PAGE
    if (
        image_count == 0
        and path_operator_count <= BLANK_PAGE_MAXIMUM_PATH_OPERATORS
        and drawing_object_count <= BLANK_PAGE_MAXIMUM_DRAWING_OBJECTS
    ):
        return BLANK_PAGE
    return VECTOR_ONLY_PAGE


def determine_status(
    *,
    readable_pdf: bool,
    encrypted: bool | None,
    page_types: list[str],
) -> str:
    """Apply the ordered document-level preflight rules."""

    if not readable_pdf:
        return "UNREADABLE_PDF"
    if encrypted:
        return "ENCRYPTED_PDF"
    if not page_types or all(page_type == BLANK_PAGE for page_type in page_types):
        return "EMPTY_DOCUMENT"
    if SCANNED_IMAGE_PAGE in page_types:
        return "SCANNED_IMAGE_PAGE_PRESENT"
    if VECTOR_ONLY_PAGE in page_types:
        return "VECTOR_ONLY_PAGE_PRESENT"
    if not any(
        page_type in {NATIVE_TEXT_PAGE, SPARSE_TEXT_PAGE}
        for page_type in page_types
    ):
        return "NO_TEXT_LAYER"
    if not all(
        page_type in LAYOUT_ELIGIBLE_PAGE_TYPES for page_type in page_types
    ):
        return "UNSUPPORTED_PAGE_TYPE"
    if SPARSE_TEXT_PAGE in page_types:
        return "BASIC_PREFLIGHT_PASSED_WITH_WARNINGS"
    return "BASIC_PREFLIGHT_PASSED"


def inspect_pdf(path: Path) -> PreflightResult:
    """Inspect one PDF without extracting Receiving Report business fields."""

    resolved_path = path.expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Input PDF does not exist: {resolved_path}")

    result = PreflightResult(
        filename=resolved_path.name,
        source_path=str(resolved_path),
        sha256=sha256_file(resolved_path),
        file_size_bytes=resolved_path.stat().st_size,
    )

    try:
        reader = PdfReader(str(resolved_path), strict=False)
        result.readable_pdf = True
        result.encrypted = reader.is_encrypted

        if result.encrypted:
            result.status = determine_status(
                readable_pdf=True,
                encrypted=True,
                page_types=[],
            )
            result.checks = {
                "readable_pdf": True,
                "not_encrypted": False,
                "native_text_layer": False,
                "no_full_page_image": False,
                "contains_native_text_page": False,
                "only_native_or_blank_pages": False,
                "only_layout_eligible_pages": False,
                "sparse_text_requires_layout_validation": False,
            }
            return result

        result.page_count = len(reader.pages)

        with pdfplumber.open(str(resolved_path)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                reader_page = reader.pages[page_number - 1]
                rotation = int(reader_page.get("/Rotate", 0) or 0) % 360
                full_page_image = any(
                    image_covers_page(image, float(page.width), float(page.height))
                    for image in page.images
                )
                text_show_operator_count, path_operator_count = count_page_operators(
                    reader_page,
                    reader,
                )
                page_type = classify_page(
                    text_character_count=len(page.chars),
                    image_count=len(page.images),
                    has_full_page_image=full_page_image,
                    path_operator_count=path_operator_count,
                    drawing_object_count=(
                        len(page.lines) + len(page.rects) + len(page.curves)
                    ),
                )

                page_result = PageInspection(
                    page_number=page_number,
                    width_points=round(float(page.width), 3),
                    height_points=round(float(page.height), 3),
                    rotation_degrees=rotation,
                    text_character_count=len(page.chars),
                    image_count=len(page.images),
                    vector_line_count=len(page.lines),
                    rectangle_count=len(page.rects),
                    curve_count=len(page.curves),
                    font_count=count_page_fonts(reader_page),
                    text_show_operator_count=text_show_operator_count,
                    path_operator_count=path_operator_count,
                    has_full_page_image=full_page_image,
                    page_type=page_type,
                )
                result.pages.append(page_result)

        result.total_text_character_count = sum(
            page.text_character_count for page in result.pages
        )
        result.has_full_page_image = any(
            page.has_full_page_image for page in result.pages
        )
        result.has_text_layer = any(
            page.text_character_count > 0 for page in result.pages
        )
        page_types = [page.page_type for page in result.pages]
        result.page_type_counts = dict(sorted(Counter(page_types).items()))
        result.blocking_pages = [
            page.page_number
            for page in result.pages
            if page.page_type not in LAYOUT_ELIGIBLE_PAGE_TYPES
        ]
        result.warning_pages = [
            page.page_number
            for page in result.pages
            if page.page_type == SPARSE_TEXT_PAGE
        ]
        result.status = determine_status(
            readable_pdf=result.readable_pdf,
            encrypted=result.encrypted,
            page_types=page_types,
        )
        contains_native_text_page = NATIVE_TEXT_PAGE in page_types
        only_native_or_blank_pages = bool(page_types) and all(
            page_type in {NATIVE_TEXT_PAGE, BLANK_PAGE}
            for page_type in page_types
        )
        only_layout_eligible_pages = bool(page_types) and all(
            page_type in LAYOUT_ELIGIBLE_PAGE_TYPES
            for page_type in page_types
        )
        result.checks = {
            "readable_pdf": result.readable_pdf,
            "not_encrypted": not bool(result.encrypted),
            "native_text_layer": result.has_text_layer,
            "no_full_page_image": not result.has_full_page_image,
            "contains_native_text_page": contains_native_text_page,
            "only_native_or_blank_pages": only_native_or_blank_pages,
            "only_layout_eligible_pages": only_layout_eligible_pages,
            "sparse_text_requires_layout_validation": bool(
                result.warning_pages
            ),
        }
        if result.warning_pages:
            page_list = ", ".join(str(page) for page in result.warning_pages)
            result.warnings.append(
                "Sparse native text requires deterministic layout validation "
                f"on page(s): {page_list}."
            )
    except Exception as error:
        result.readable_pdf = False
        result.status = "UNREADABLE_PDF"
        result.errors.append(f"{type(error).__name__}: {error}")
        result.checks = {
            "readable_pdf": False,
            "not_encrypted": False,
            "native_text_layer": False,
            "no_full_page_image": False,
            "contains_native_text_page": False,
            "only_native_or_blank_pages": False,
            "only_layout_eligible_pages": False,
            "sparse_text_requires_layout_validation": False,
        }

    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic basic preflight checks on one RR PDF."
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
        result = inspect_pdf(args.pdf)
    except (FileNotFoundError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1

    output_text = json.dumps(result.to_dict(), indent=2, sort_keys=True)
    if args.output:
        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text + "\n", encoding="utf-8")
        print(f"Preflight result written to: {output_path}")
    else:
        print(output_text)

    return 0 if result.status in PREFLIGHT_PASS_STATUSES else 2


if __name__ == "__main__":
    raise SystemExit(main())
