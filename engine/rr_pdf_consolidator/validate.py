"""Deterministic validation and type normalization for extracted RR data."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from rr_pdf_consolidator.extract import (
    DescriptionRecord,
    RRExtractionResult,
    inspect_rr,
)
from rr_pdf_consolidator.layout import INPUT_CONTRACT
from rr_pdf_consolidator.table_extract import TOTAL_ROW


VALIDATION_SCHEMA_VERSION = "RR_VALIDATION_V9"
DATE_PATTERN = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")
DECIMAL_PATTERN = re.compile(
    r"^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$"
)
NUMERIC_FIELDS = (
    "kilos",
    "less_cage_or_pallets",
    "net_weight",
    "qty_pcs",
)
REQUIRED_DETAIL_NUMERIC_FIELDS = (
    "kilos",
    "net_weight",
    "qty_pcs",
)
REQUIRED_PRINTED_TOTAL_FIELDS = (
    "kilos",
    "net_weight",
    "qty_pcs",
)
SECTION_METADATA_FIELDS = (
    "rr_table_date",
    "rr_enviro_ref_rr_no",
    "rr_company_name",
    "rr_receiving_notes",
    "rr_pis_no",
    "rr_expenses",
    "rr_manpower",
    "rr_trucking_1",
    "rr_trucking_2",
    "rr_purchased_cost",
)


@dataclass(frozen=True)
class ValidationCheck:
    """One explicit pass/fail control."""

    name: str
    passed: bool
    message: str


@dataclass(frozen=True)
class ValidatedDescriptionRecord:
    """One database-grain record with normalized dates and exact decimals."""

    record_number: int
    source_filename: str
    source_sha256: str
    template_id: str
    source_page_number: int
    source_table_row_number: int
    rr_order_type: str | None
    rr_reference_no: str
    rr_received_date_iso: str | None
    rr_job_no: str | None
    rr_schedule_date_iso: str | None
    rr_account_rep: str | None
    rr_table_date_iso: str | None
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
    kilos_decimal: str
    less_cage_or_pallets_decimal: str | None
    net_weight_decimal: str
    qty_pcs_decimal: str
    uom: str | None
    category: str | None
    remarks: str | None


@dataclass
class RRValidationResult:
    """Machine-readable validation and quarantine decision."""

    filename: str
    source_path: str
    sha256: str
    extraction_status: str
    input_contract: str = INPUT_CONTRACT
    validation_schema_version: str = VALIDATION_SCHEMA_VERSION
    status: str = "EXTRACTION_REJECTED"
    quarantine_required: bool = True
    checks: list[ValidationCheck] = field(default_factory=list)
    computed_totals: dict[str, str] = field(default_factory=dict)
    printed_totals: dict[str, str | None] = field(default_factory=dict)
    validated_record_count: int = 0
    validated_records: list[ValidatedDescriptionRecord] = field(
        default_factory=list
    )
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_source_date(value: str | None) -> date | None:
    """Parse M/D/YY or M/D/YYYY, assigning two-digit years to 2000-2099."""

    if value is None:
        return None
    match = DATE_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"Unsupported date format: {value!r}")

    month = int(match.group(1))
    day = int(match.group(2))
    year_text = match.group(3)
    year = int(year_text)
    if len(year_text) == 2:
        year += 2000
    return date(year, month, day)


def parse_source_decimal(value: str | None) -> Decimal | None:
    """Parse a nonnegative source decimal without binary floating point."""

    if value is None:
        return None
    stripped = value.strip()
    if not DECIMAL_PATTERN.fullmatch(stripped):
        raise ValueError(f"Unsupported decimal format: {value!r}")
    try:
        return Decimal(stripped.replace(",", ""))
    except InvalidOperation as error:
        raise ValueError(f"Invalid decimal value: {value!r}") from error


def decimal_string(value: Decimal) -> str:
    """Return a stable non-exponent decimal string."""

    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def add_check(
    result: RRValidationResult,
    *,
    name: str,
    passed: bool,
    message: str,
) -> None:
    """Append one check and promote failures to the error list."""

    result.checks.append(
        ValidationCheck(
            name=name,
            passed=passed,
            message=message,
        )
    )
    if not passed:
        result.errors.append(f"{name}: {message}")


def parse_record_numbers(
    records: list[DescriptionRecord],
) -> tuple[dict[int, dict[str, Decimal | None]], list[str]]:
    """Parse every detail numeric cell and report exact locations of failures."""

    parsed: dict[int, dict[str, Decimal | None]] = {}
    errors: list[str] = []
    for record in records:
        values: dict[str, Decimal | None] = {}
        for field_name in NUMERIC_FIELDS:
            raw_value = getattr(record, field_name)
            if (
                field_name in REQUIRED_DETAIL_NUMERIC_FIELDS
                and raw_value is None
            ):
                errors.append(
                    f"record {record.record_number} field {field_name} is blank"
                )
                values[field_name] = None
                continue
            try:
                values[field_name] = parse_source_decimal(raw_value)
            except ValueError as error:
                errors.append(
                    f"record {record.record_number} field {field_name}: {error}"
                )
                values[field_name] = None
        parsed[record.record_number] = values
    return parsed, errors


def validate_extraction(
    extraction: RRExtractionResult,
) -> RRValidationResult:
    """Apply all deterministic controls to one extraction result."""

    result = RRValidationResult(
        filename=extraction.filename,
        source_path=extraction.source_path,
        sha256=extraction.sha256,
        extraction_status=extraction.status,
        warnings=list(extraction.warnings),
    )
    extraction_passed = extraction.status == "RR_EXTRACTION_SUCCEEDED"
    add_check(
        result,
        name="extraction_succeeded",
        passed=extraction_passed,
        message=(
            "Step 3 extraction succeeded."
            if extraction_passed
            else f"Step 3 status is {extraction.status}."
        ),
    )
    if not extraction_passed:
        result.status = "EXTRACTION_REJECTED"
        result.errors.extend(extraction.errors)
        return result

    records = extraction.description_records
    add_check(
        result,
        name="description_records_present",
        passed=bool(records),
        message=(
            f"{len(records)} description record(s) are present."
            if records
            else "No description records are present."
        ),
    )

    header_reference = extraction.header_values.get("reference_no")
    table_reference = extraction.table_metadata_values.get(
        "rr_enviro_ref_rr_no"
    )
    references_match = (
        header_reference is not None
        and table_reference is not None
        and table_reference.strip() == header_reference.strip()
    )
    add_check(
        result,
        name="reference_numbers_consistent",
        passed=references_match,
        message=(
            f"Header and table reference number are {header_reference!r}."
            if references_match
            else (
                f"Header reference is {header_reference!r}; "
                f"table reference is {table_reference!r}."
            )
        ),
    )

    section_reference_errors: list[str] = []
    if header_reference is None:
        section_reference_errors.append("Header reference is blank.")
    else:
        section_reference_pattern = re.compile(
            rf"^{re.escape(header_reference.strip())}"
            r"(?:\s*\(\s*\d+\s*\))?$"
        )
        for section in extraction.rr_sections:
            section_reference = section.metadata_values.get(
                "rr_enviro_ref_rr_no"
            )
            if (
                section_reference is None
                or section_reference_pattern.fullmatch(
                    section_reference.strip()
                )
                is None
            ):
                section_reference_errors.append(
                    f"section {section.section_number} reference "
                    f"{section_reference!r} does not belong to header "
                    f"reference {header_reference!r}"
                )
    add_check(
        result,
        name="section_reference_numbers_consistent",
        passed=not section_reference_errors,
        message=(
            f"{len(extraction.rr_sections)} RR section reference(s) "
            "match the header reference or its numbered suffix."
            if not section_reference_errors
            else "; ".join(section_reference_errors)
        ),
    )

    received_date = None
    schedule_date = None
    date_errors: list[str] = []
    for field_name in ("received_date", "schedule_date"):
        raw_value = extraction.header_values.get(field_name)
        try:
            parsed_value = parse_source_date(raw_value)
        except ValueError as error:
            date_errors.append(f"{field_name}: {error}")
            parsed_value = None
        if field_name == "received_date":
            received_date = parsed_value
        else:
            schedule_date = parsed_value
    add_check(
        result,
        name="header_dates_valid",
        passed=not date_errors,
        message=(
            "Header dates are blank or valid M/D/YY or M/D/YYYY values."
            if not date_errors
            else "; ".join(date_errors)
        ),
    )

    first_table_date = extraction.table_metadata_values.get("rr_table_date")
    parsed_table_date = None
    if received_date is None:
        table_date_matches = True
        table_date_message = (
            "Not applicable because received_date is blank or invalid."
        )
    else:
        try:
            parsed_table_date = parse_source_date(first_table_date)
            table_date_matches = parsed_table_date == received_date
            table_date_message = (
                f"First table date {first_table_date!r} "
                f"{'matches' if table_date_matches else 'does not match'} "
                f"received_date {received_date.isoformat()!r}."
            )
        except ValueError as error:
            table_date_matches = False
            table_date_message = f"First table date is invalid: {error}"
    add_check(
        result,
        name="first_table_date_matches_received_date",
        passed=table_date_matches,
        message=table_date_message,
    )

    parsed_record_table_dates: dict[int, date | None] = {}
    section_date_errors: list[str] = []
    for record in records:
        try:
            parsed_record_table_dates[record.record_number] = (
                parse_source_date(record.rr_table_date)
            )
        except ValueError as error:
            parsed_record_table_dates[record.record_number] = None
            section_date_errors.append(
                f"record {record.record_number}: {error}"
            )
    add_check(
        result,
        name="section_table_dates_valid",
        passed=not section_date_errors,
        message=(
            "Every section table date is blank or a valid source date."
            if not section_date_errors
            else "; ".join(section_date_errors)
        ),
    )

    lineage_matches = all(
        record.source_filename == extraction.filename
        and record.source_sha256 == extraction.sha256
        and record.rr_reference_no == header_reference
        and record.template_id == extraction.template_id
        for record in records
    )
    add_check(
        result,
        name="record_lineage_consistent",
        passed=lineage_matches,
        message=(
            "Every description record shares the extraction lineage."
            if lineage_matches
            else "One or more records have inconsistent source lineage."
        ),
    )

    section_errors: list[str] = []
    section_record_numbers: set[int] = set()
    if not extraction.rr_sections:
        section_errors.append("No RR section evidence is present.")
    for section in extraction.rr_sections:
        start_coordinate = (
            section.start_page_number,
            section.start_physical_row_number,
        )
        end_coordinate = (
            section.end_page_number,
            section.end_physical_row_number,
        )
        section_records = [
            record
            for record in records
            if start_coordinate
            <= (
                record.source_page_number,
                record.source_table_row_number,
            )
            <= end_coordinate
        ]
        if len(section_records) != section.description_row_count:
            section_errors.append(
                f"RR section {section.section_number} expects "
                f"{section.description_row_count} item row(s), found "
                f"{len(section_records)}."
            )
        for record in section_records:
            if record.record_number in section_record_numbers:
                section_errors.append(
                    f"record {record.record_number} occurs in overlapping "
                    "RR sections."
                )
            section_record_numbers.add(record.record_number)
            for field_name in SECTION_METADATA_FIELDS:
                expected = section.metadata_values.get(field_name)
                actual = getattr(record, field_name)
                if actual != expected:
                    section_errors.append(
                        f"record {record.record_number} field "
                        f"{field_name} is {actual!r}; expected "
                        f"{expected!r} from section "
                        f"{section.section_number}."
                    )
    missing_section_records = sorted(
        record.record_number
        for record in records
        if record.record_number not in section_record_numbers
    )
    if missing_section_records:
        section_errors.append(
            "Records outside every RR section: "
            + ", ".join(
                str(number) for number in missing_section_records
            )
        )
    add_check(
        result,
        name="rr_section_assignments_consistent",
        passed=not section_errors,
        message=(
            f"{len(extraction.rr_sections)} RR section(s) cover every "
            "description record with matching metadata."
            if not section_errors
            else "; ".join(section_errors)
        ),
    )

    pis_errors: list[str] = []
    assigned_record_numbers: set[int] = set()
    if not extraction.pis_groups:
        pis_errors.append("No PIS group evidence is present.")
    for group in extraction.pis_groups:
        start_coordinate = (
            group.start_page_number,
            group.start_physical_row_number,
        )
        end_coordinate = (
            group.end_page_number,
            group.end_physical_row_number,
        )
        group_records = [
            record
            for record in records
            if start_coordinate
            <= (
                record.source_page_number,
                record.source_table_row_number,
            )
            <= end_coordinate
        ]
        if len(group_records) != group.description_row_count:
            pis_errors.append(
                f"PIS group {group.group_number} expects "
                f"{group.description_row_count} item row(s), found "
                f"{len(group_records)}."
            )
        for record in group_records:
            if record.record_number in assigned_record_numbers:
                pis_errors.append(
                    f"record {record.record_number} occurs in overlapping "
                    "PIS groups."
                )
            assigned_record_numbers.add(record.record_number)
            if record.rr_pis_no != group.pis_no:
                pis_errors.append(
                    f"record {record.record_number} has PIS "
                    f"{record.rr_pis_no!r}; expected {group.pis_no!r}."
                )
    missing_pis_records = sorted(
        record.record_number
        for record in records
        if record.record_number not in assigned_record_numbers
    )
    if missing_pis_records:
        pis_errors.append(
            "Records outside every PIS group: "
            + ", ".join(str(number) for number in missing_pis_records)
        )
    add_check(
        result,
        name="pis_group_assignments_consistent",
        passed=not pis_errors,
        message=(
            f"{len(extraction.pis_groups)} PIS group(s) cover every "
            "description record exactly once."
            if not pis_errors
            else "; ".join(pis_errors)
        ),
    )

    parsed_records, numeric_errors = parse_record_numbers(records)
    add_check(
        result,
        name="detail_numeric_fields_valid",
        passed=not numeric_errors,
        message=(
            "All required detail numeric fields are valid nonnegative decimals."
            if not numeric_errors
            else "; ".join(numeric_errors)
        ),
    )

    arithmetic_errors: list[str] = []
    if not numeric_errors:
        for record in records:
            parsed = parsed_records[record.record_number]
            kilos = parsed["kilos"]
            less = parsed["less_cage_or_pallets"] or Decimal("0")
            net_weight = parsed["net_weight"]
            if (
                kilos is not None
                and net_weight is not None
                and kilos - less != net_weight
            ):
                arithmetic_errors.append(
                    f"record {record.record_number}: "
                    f"{decimal_string(kilos)} - {decimal_string(less)} "
                    f"!= {decimal_string(net_weight)}"
                )
    else:
        arithmetic_errors.append(
            "Detail arithmetic was not evaluated because numeric parsing failed."
        )
    add_check(
        result,
        name="detail_weight_arithmetic_valid",
        passed=not arithmetic_errors,
        message=(
            "Every detail row satisfies kilos - less = net_weight."
            if not arithmetic_errors
            else "; ".join(arithmetic_errors)
        ),
    )

    total_rows = [
        row for row in extraction.table_rows if row.row_type == TOTAL_ROW
    ]
    single_total = len(total_rows) == 1
    add_check(
        result,
        name="single_printed_total_row_present",
        passed=single_total,
        message=f"Found {len(total_rows)} printed total row(s).",
    )

    printed_values: dict[str, Decimal | None] = {
        field_name: None for field_name in NUMERIC_FIELDS
    }
    printed_errors: list[str] = []
    if single_total:
        total_cells = total_rows[0].cells
        for field_name in NUMERIC_FIELDS:
            raw_value = total_cells.get(field_name)
            if (
                field_name in REQUIRED_PRINTED_TOTAL_FIELDS
                and raw_value is None
            ):
                printed_errors.append(
                    f"printed total field {field_name} is blank"
                )
                continue
            try:
                printed_values[field_name] = parse_source_decimal(raw_value)
            except ValueError as error:
                printed_errors.append(
                    f"printed total field {field_name}: {error}"
                )
    else:
        printed_errors.append(
            "Printed totals were not parsed because exactly one total row "
            "was not found."
        )
    add_check(
        result,
        name="printed_totals_valid",
        passed=not printed_errors,
        message=(
            "Printed totals are valid nonnegative decimals."
            if not printed_errors
            else "; ".join(printed_errors)
        ),
    )

    computed_values = {
        field_name: Decimal("0") for field_name in NUMERIC_FIELDS
    }
    if not numeric_errors:
        for parsed in parsed_records.values():
            for field_name in NUMERIC_FIELDS:
                computed_values[field_name] += (
                    parsed[field_name] or Decimal("0")
                )
    result.computed_totals = {
        field_name: decimal_string(value)
        for field_name, value in computed_values.items()
    }
    result.printed_totals = {
        field_name: (
            decimal_string(value) if value is not None else None
        )
        for field_name, value in printed_values.items()
    }

    total_mismatches: list[str] = []
    if not numeric_errors and not printed_errors:
        for field_name in NUMERIC_FIELDS:
            printed = printed_values[field_name] or Decimal("0")
            computed = computed_values[field_name]
            if computed != printed:
                total_mismatches.append(
                    f"{field_name}: computed {decimal_string(computed)} "
                    f"!= printed {decimal_string(printed)}"
                )
    else:
        total_mismatches.append(
            "Totals reconciliation was not evaluated because numeric "
            "validation failed."
        )
    add_check(
        result,
        name="printed_totals_reconcile",
        passed=not total_mismatches,
        message=(
            "Computed detail totals exactly match the printed totals."
            if not total_mismatches
            else "; ".join(total_mismatches)
        ),
    )

    if result.errors:
        result.status = "VALIDATION_FAILED"
        result.quarantine_required = True
        return result

    for record in records:
        parsed = parsed_records[record.record_number]
        kilos = parsed["kilos"]
        net_weight = parsed["net_weight"]
        qty_pcs = parsed["qty_pcs"]
        if kilos is None or net_weight is None or qty_pcs is None:
            raise AssertionError("Validated required decimals cannot be null.")
        result.validated_records.append(
            ValidatedDescriptionRecord(
                record_number=record.record_number,
                source_filename=record.source_filename,
                source_sha256=record.source_sha256,
                template_id=record.template_id,
                source_page_number=record.source_page_number,
                source_table_row_number=record.source_table_row_number,
                rr_order_type=record.rr_order_type,
                rr_reference_no=record.rr_reference_no,
                rr_received_date_iso=(
                    received_date.isoformat()
                    if received_date is not None
                    else None
                ),
                rr_job_no=record.rr_job_no,
                rr_schedule_date_iso=(
                    schedule_date.isoformat()
                    if schedule_date is not None
                    else None
                ),
                rr_account_rep=record.rr_account_rep,
                rr_table_date_iso=(
                    parsed_record_table_dates[
                        record.record_number
                    ].isoformat()
                    if parsed_record_table_dates[
                        record.record_number
                    ]
                    is not None
                    else None
                ),
                rr_enviro_ref_rr_no=record.rr_enviro_ref_rr_no,
                rr_company_name=record.rr_company_name,
                rr_receiving_notes=record.rr_receiving_notes,
                rr_pis_no=record.rr_pis_no,
                rr_expenses=record.rr_expenses,
                rr_manpower=record.rr_manpower,
                rr_trucking_1=record.rr_trucking_1,
                rr_trucking_2=record.rr_trucking_2,
                rr_purchased_cost=record.rr_purchased_cost,
                description=record.description,
                kilos_decimal=decimal_string(kilos),
                less_cage_or_pallets_decimal=(
                    decimal_string(parsed["less_cage_or_pallets"])
                    if parsed["less_cage_or_pallets"] is not None
                    else None
                ),
                net_weight_decimal=decimal_string(net_weight),
                qty_pcs_decimal=decimal_string(qty_pcs),
                uom=record.uom,
                category=record.category,
                remarks=record.remarks,
            )
        )

    result.validated_record_count = len(result.validated_records)
    result.status = "VALIDATION_PASSED"
    result.quarantine_required = False
    return result


def inspect_validation(path: Path) -> RRValidationResult:
    """Run Steps 1-3, then validate and normalize the extracted records."""

    return validate_extraction(inspect_rr(path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic RR extraction validation."
    )
    parser.add_argument("pdf", type=Path, help="Path to the PDF to validate.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the JSON result. Otherwise prints to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result = inspect_validation(args.pdf)
    except (FileNotFoundError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1

    output_text = json.dumps(result.to_dict(), indent=2, sort_keys=True)
    if args.output:
        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text + "\n", encoding="utf-8")
        print(f"RR validation result written to: {output_path}")
    else:
        print(output_text)

    return 0 if result.status == "VALIDATION_PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
