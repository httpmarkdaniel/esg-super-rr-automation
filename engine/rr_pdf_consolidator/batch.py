"""Deterministic folder-level RR processing and consolidation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from rr_pdf_consolidator.preflight import sha256_file
from rr_pdf_consolidator.validate import (
    RRValidationResult,
    ValidatedDescriptionRecord,
    inspect_validation,
)


ACCEPTED = "ACCEPTED"
QUARANTINED = "QUARANTINED"
DUPLICATE_SKIPPED = "DUPLICATE_SKIPPED"
REFERENCE_CONFLICT = "REFERENCE_CONFLICT"


@dataclass(frozen=True)
class BatchItem:
    """Manifest entry for one discovered PDF."""

    source_relative_path: str
    sha256: str
    decision: str
    validation_status: str
    reference_no: str | None
    validated_record_count: int
    included_record_count: int
    report_relative_path: str
    duplicate_of: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class BatchManifest:
    """Stable batch manifest and record-count summary."""

    input_directory: str
    output_directory: str
    recursive: bool
    status: str = "BATCH_NOT_RUN"
    pdf_discovered_count: int = 0
    unique_hash_count: int = 0
    accepted_file_count: int = 0
    quarantined_file_count: int = 0
    duplicate_skipped_count: int = 0
    consolidated_record_count: int = 0
    items: list[BatchItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def path_is_within(path: Path, parent: Path) -> bool:
    """Return True when path is parent or one of its descendants."""

    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def discover_pdfs(
    input_directory: Path,
    *,
    recursive: bool,
    excluded_directory: Path | None = None,
) -> list[Path]:
    """Discover PDF files in stable relative-path order."""

    pattern = "**/*" if recursive else "*"
    excluded = (
        excluded_directory.expanduser().resolve()
        if excluded_directory is not None
        else None
    )
    paths = []
    for path in input_directory.glob(pattern):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        resolved = path.resolve()
        if excluded is not None and path_is_within(resolved, excluded):
            continue
        paths.append(resolved)

    return sorted(
        paths,
        key=lambda path: (
            path.relative_to(input_directory).as_posix().casefold(),
            path.relative_to(input_directory).as_posix(),
        ),
    )


def ensure_empty_output_directory(output_directory: Path) -> None:
    """Create an output directory, refusing to mix with existing files."""

    if output_directory.exists():
        if not output_directory.is_dir():
            raise NotADirectoryError(
                f"Batch output path is not a directory: {output_directory}"
            )
        if any(output_directory.iterdir()):
            raise FileExistsError(
                "Batch output directory must be empty: "
                f"{output_directory}"
            )
    else:
        output_directory.mkdir(parents=True, exist_ok=False)


def write_json(path: Path, value: Any) -> None:
    """Write stable, human-readable JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def record_uid(record: ValidatedDescriptionRecord) -> str:
    """Build a stable idempotency key from immutable source coordinates."""

    identity = (
        f"{record.source_sha256}:"
        f"{record.source_page_number}:"
        f"{record.source_table_row_number}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest().upper()


def record_to_batch_row(
    record: ValidatedDescriptionRecord,
    source_relative_path: str,
) -> dict[str, Any]:
    """Add batch lineage and a stable key to one normalized record."""

    return {
        "record_uid": record_uid(record),
        "source_relative_path": source_relative_path,
        **asdict(record),
    }


def write_consolidated_records(
    output_directory: Path,
    rows: list[dict[str, Any]],
) -> None:
    """Write database-grain rows as deterministic CSV and JSONL."""

    record_fields = [
        field_definition.name
        for field_definition in fields(ValidatedDescriptionRecord)
    ]
    fieldnames = [
        "record_uid",
        "source_relative_path",
        *record_fields,
    ]

    csv_path = output_directory / "validated-records.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=fieldnames,
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    jsonl_path = output_directory / "validated-records.jsonl"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True) + "\n")


def report_path_for(
    relative_source: str,
    decision: str,
) -> Path:
    """Return a collision-safe report path that mirrors the source tree."""

    if decision == ACCEPTED:
        category = "passed"
    elif decision == DUPLICATE_SKIPPED:
        category = "skipped"
    else:
        category = "quarantine"
    relative_path = Path(relative_source)
    report_name = relative_path.name + ".result.json"
    return Path("reports") / category / relative_path.parent / report_name


def validation_reference(result: RRValidationResult) -> str | None:
    """Return the validated RR reference number, if available."""

    if not result.validated_records:
        return None
    return result.validated_records[0].rr_reference_no


def run_batch(
    input_directory: Path,
    output_directory: Path,
    *,
    recursive: bool = False,
    validator: Callable[[Path], RRValidationResult] = inspect_validation,
) -> BatchManifest:
    """Validate every unique PDF and consolidate only unambiguous passes."""

    input_root = input_directory.expanduser().resolve()
    output_root = output_directory.expanduser().resolve()
    if not input_root.is_dir():
        raise NotADirectoryError(
            f"Batch input directory does not exist: {input_root}"
        )

    discovered = discover_pdfs(
        input_root,
        recursive=recursive,
        excluded_directory=output_root,
    )
    ensure_empty_output_directory(output_root)
    manifest = BatchManifest(
        input_directory=str(input_root),
        output_directory=str(output_root),
        recursive=recursive,
        pdf_discovered_count=len(discovered),
    )
    if not discovered:
        manifest.status = "NO_PDF_FILES"
        manifest.errors.append("No PDF files were discovered.")
        write_consolidated_records(output_root, [])
        write_json(output_root / "manifest.json", manifest.to_dict())
        return manifest

    work_items: list[dict[str, Any]] = []
    canonical_by_hash: dict[str, str] = {}
    for path in discovered:
        relative = path.relative_to(input_root).as_posix()
        digest = sha256_file(path)
        duplicate_of = canonical_by_hash.get(digest)
        if duplicate_of is None:
            canonical_by_hash[digest] = relative
        work_items.append(
            {
                "path": path,
                "relative": relative,
                "sha256": digest,
                "duplicate_of": duplicate_of,
                "validation": None,
                "decision": (
                    DUPLICATE_SKIPPED
                    if duplicate_of is not None
                    else None
                ),
                "batch_errors": [],
            }
        )
    manifest.unique_hash_count = len(canonical_by_hash)

    for item in work_items:
        if item["decision"] == DUPLICATE_SKIPPED:
            continue
        validation = validator(item["path"])
        item["validation"] = validation
        if validation.sha256 != item["sha256"]:
            item["decision"] = QUARANTINED
            item["batch_errors"].append(
                "Source SHA-256 changed between batch discovery and "
                "validation."
            )
        elif validation.status == "VALIDATION_PASSED":
            item["decision"] = ACCEPTED
        else:
            item["decision"] = QUARANTINED
            item["batch_errors"].extend(validation.errors)

    reference_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in work_items:
        validation = item["validation"]
        if item["decision"] != ACCEPTED or validation is None:
            continue
        reference = validation_reference(validation)
        if reference is not None:
            reference_groups[reference].append(item)

    for reference, group in sorted(reference_groups.items()):
        if len(group) <= 1:
            continue
        conflict_paths = sorted(item["relative"] for item in group)
        message = (
            f"RR reference {reference!r} occurs in different source PDFs: "
            + ", ".join(conflict_paths)
        )
        for item in group:
            item["decision"] = REFERENCE_CONFLICT
            item["batch_errors"].append(message)

    consolidated_rows: list[dict[str, Any]] = []
    manifest_items: list[BatchItem] = []
    for item in work_items:
        decision = item["decision"]
        validation = item["validation"]
        report_relative = report_path_for(item["relative"], decision)
        reference = (
            validation_reference(validation)
            if validation is not None
            else None
        )
        validated_count = (
            validation.validated_record_count
            if validation is not None
            else 0
        )
        included_count = validated_count if decision == ACCEPTED else 0

        report = {
            "source_relative_path": item["relative"],
            "sha256": item["sha256"],
            "batch_decision": decision,
            "duplicate_of": item["duplicate_of"],
            "reference_no": reference,
            "batch_errors": item["batch_errors"],
            "validation": (
                validation.to_dict()
                if validation is not None
                else None
            ),
        }
        write_json(output_root / report_relative, report)

        if decision == ACCEPTED and validation is not None:
            for record in validation.validated_records:
                consolidated_rows.append(
                    record_to_batch_row(record, item["relative"])
                )

        manifest_items.append(
            BatchItem(
                source_relative_path=item["relative"],
                sha256=item["sha256"],
                decision=decision,
                validation_status=(
                    validation.status
                    if validation is not None
                    else "NOT_RUN_DUPLICATE"
                ),
                reference_no=reference,
                validated_record_count=validated_count,
                included_record_count=included_count,
                report_relative_path=report_relative.as_posix(),
                duplicate_of=item["duplicate_of"],
                errors=list(item["batch_errors"]),
            )
        )

    manifest.items = manifest_items
    manifest.accepted_file_count = sum(
        item.decision == ACCEPTED for item in manifest.items
    )
    manifest.quarantined_file_count = sum(
        item.decision in {QUARANTINED, REFERENCE_CONFLICT}
        for item in manifest.items
    )
    manifest.duplicate_skipped_count = sum(
        item.decision == DUPLICATE_SKIPPED for item in manifest.items
    )
    manifest.consolidated_record_count = len(consolidated_rows)
    manifest.status = (
        "BATCH_COMPLETED"
        if (
            manifest.quarantined_file_count == 0
            and manifest.duplicate_skipped_count == 0
        )
        else "BATCH_COMPLETED_WITH_EXCEPTIONS"
    )

    write_consolidated_records(output_root, consolidated_rows)
    write_json(output_root / "manifest.json", manifest.to_dict())
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a folder of one-RR-per-PDF inputs and consolidate passes."
        )
    )
    parser.add_argument(
        "input_directory",
        type=Path,
        help="Directory containing individual RR PDFs.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New or empty directory for the manifest and consolidated rows.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Discover PDFs recursively below the input directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        manifest = run_batch(
            args.input_directory,
            args.output_dir,
            recursive=args.recursive,
        )
    except (FileNotFoundError, NotADirectoryError, FileExistsError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "status": manifest.status,
                "pdf_discovered_count": manifest.pdf_discovered_count,
                "accepted_file_count": manifest.accepted_file_count,
                "quarantined_file_count": manifest.quarantined_file_count,
                "duplicate_skipped_count": manifest.duplicate_skipped_count,
                "consolidated_record_count": (
                    manifest.consolidated_record_count
                ),
                "output_directory": manifest.output_directory,
            },
            indent=2,
            sort_keys=True,
        )
    )

    return 0 if manifest.status == "BATCH_COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
