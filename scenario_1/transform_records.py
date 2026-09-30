"""Transform a customer HRIS export into CandorIQ's employee ingest columns.

Usage:
    python scenario_1/transform_records.py raw.csv employees.csv \
        --config scenario_1/configs/example_customer.json \
        --effective-date 2026-09-29 --report exceptions.json \
        [--rejected rejected_rows.csv] [--allow-partial]

Every customer-specific decision (column names, name/date/salary formats,
department and location lookups, currency) lives in the config; see README.md.
The ingest file is written only when there are no blocking errors, unless
--allow-partial is passed after CS has approved loading the clean rows.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


SCRIPT_VERSION = "2.0.0"
OUTPUT_FIELDS = (
    "employee_id", "first_name", "last_name", "department", "job_title",
    "manager_id", "hire_date", "termination_date", "base_salary",
    "currency", "location", "employment_status",
)
BASE_CONCEPTS = (
    "employee_id", "department_code", "job_title", "manager_id",
    "hire_date", "termination_date", "base_salary", "location",
)
NAME_CONCEPTS = {
    "last_first": ("full_name",),
    "first_last": ("full_name",),
    "separate_columns": ("first_name", "last_name"),
}
DEFAULT_SALARY_RANGE = (10_000, 2_000_000)


@dataclass(frozen=True)
class Spec:
    """Validated, customer-confirmed transform settings."""
    fields: dict[str, str]
    name_format: str
    date_format: str
    delimiter: str
    departments: dict[str, str]
    locations: dict[str, dict[str, str | None]]
    currency: str
    salary_pattern: re.Pattern
    salary_range: tuple[Decimal, Decimal]


@dataclass
class Result:
    rows: list[dict[str, str]]
    rejected: list[dict[str, str]]
    report: dict
    source_headers: list[str]


def issue(row_numbers: list[int], employee_id: str | None, code: str, message: str) -> dict:
    return {
        "source_rows": row_numbers,
        "employee_id": employee_id or None,
        "code": code,
        "message": message,
    }


def _string_map(value, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not k or not isinstance(v, str) or not v.strip()
        for k, v in value.items()
    ):
        raise ValueError(f"{name} must map nonempty strings to nonempty strings")
    return value


def load_spec(config: dict) -> Spec:
    name_format = config.get("name_format", "last_first")
    if name_format not in NAME_CONCEPTS:
        raise ValueError(f"name_format must be one of: {', '.join(NAME_CONCEPTS)}")
    concepts = set(BASE_CONCEPTS + NAME_CONCEPTS[name_format])
    fields = config.get("field_map")
    if not isinstance(fields, dict) or set(fields) != concepts or any(
        not isinstance(v, str) or not v for v in fields.values()
    ):
        raise ValueError(f"field_map must map exactly these concepts to source columns: {', '.join(sorted(concepts))}")
    if len(set(fields.values())) != len(fields):
        raise ValueError("source columns in field_map must be distinct")

    departments = _string_map(config.get("department_map"), "department_map")
    if not departments:
        raise ValueError("a nonempty, confirmed department_map is required")

    locations: dict[str, dict[str, str | None]] = {}
    for raw, target in config.get("location_map", {}).items():
        if isinstance(target, str):
            target = {"location": target}
        if not isinstance(target, dict) or not isinstance(target.get("location"), str) or not target["location"].strip():
            raise ValueError(f"location_map entry for {raw!r} needs a nonempty location")
        locations[raw] = {"location": target["location"], "country": target.get("country")}

    currency = config.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("a confirmed three-letter currency code is required")

    date_format = config.get("date_format", "%m/%d/%Y")
    probe = date(2001, 2, 3)
    try:
        valid_format = datetime.strptime(probe.strftime(date_format), date_format).date() == probe
    except (TypeError, ValueError):
        valid_format = False
    if not valid_format:
        raise ValueError("date_format must be a strptime format containing day, month, and year")

    delimiter = config.get("delimiter", ",")
    if not isinstance(delimiter, str) or len(delimiter) != 1:
        raise ValueError("delimiter must be a single character")

    symbol = config.get("salary_symbol", "$")
    if not isinstance(symbol, str):
        raise ValueError("salary_symbol must be a string (empty for bare numbers)")
    salary_pattern = re.compile(rf"^(?:{re.escape(symbol)})?(?:\d+|\d{{1,3}}(?:,\d{{3}})+)(?:\.\d{{1,2}})?$")

    salary_range = config.get("salary_range", DEFAULT_SALARY_RANGE)
    if (not isinstance(salary_range, (list, tuple)) or len(salary_range) != 2
            or not all(isinstance(v, (int, float)) for v in salary_range)
            or salary_range[0] >= salary_range[1]):
        raise ValueError("salary_range must be [min, max] with min < max")

    return Spec(
        fields=fields, name_format=name_format, date_format=date_format,
        delimiter=delimiter, departments=departments, locations=locations,
        currency=currency, salary_pattern=salary_pattern,
        salary_range=(Decimal(str(salary_range[0])), Decimal(str(salary_range[1]))),
    )


def split_name(row: dict[str, str], spec: Spec) -> tuple[str, str, str | None]:
    """Return (first, last, error). Ambiguous names are rejected, never guessed."""
    if spec.name_format == "separate_columns":
        first, last = row[spec.fields["first_name"]], row[spec.fields["last_name"]]
    else:
        name = row[spec.fields["full_name"]]
        if spec.name_format == "last_first":
            if name.count(",") != 1:
                return "", "", "full name must be 'Last, First' with exactly one comma"
            last, first = (part.strip() for part in name.split(","))
        else:
            parts = name.split()
            if len(parts) != 2 or "," in name:
                return "", "", "full name must be exactly 'First Last'; multi-part names need review"
            first, last = parts
    if not first or not last:
        return "", "", "first and last name are required"
    return first, last, None


def parse_date(value: str, fmt: str) -> date:
    return datetime.strptime(value, fmt).date()


def swapped_day_month(fmt: str) -> str | None:
    if "%m" not in fmt or "%d" not in fmt:
        return None
    return fmt.replace("%m", "\0").replace("%d", "%m").replace("\0", "%d")


def dates_are_ambiguous(values: list[str], fmt: str) -> bool:
    """True when every date also parses with day and month swapped, and some would change."""
    swapped = swapped_day_month(fmt)
    if swapped is None:
        return False
    differs = False
    for value in values:
        try:
            as_configured = parse_date(value, fmt)
            as_swapped = parse_date(value, swapped)
        except ValueError:
            return False
        differs = differs or as_configured != as_swapped
    return differs


def transform_record(
    row: dict[str, str], spec: Spec, effective_date: date,
) -> tuple[dict[str, str] | None, list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (record, errors, warnings). Every problem in the row is collected in one pass."""
    errors: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []

    def value(concept: str) -> str:
        return row[spec.fields[concept]]

    employee_id = value("employee_id")
    first_name, last_name, name_error = split_name(row, spec)
    if name_error:
        errors.append(("invalid_name", name_error))

    code = value("department_code")
    if code not in spec.departments:
        errors.append(("unmapped_department", f"unmapped department code: {code or '<blank>'}"))
    for concept in ("job_title", "location"):
        if not value(concept):
            errors.append(("missing_value", f"{concept} is required"))

    location = value("location")
    if location and spec.locations:
        if location in spec.locations:
            location = spec.locations[location]["location"]
        else:
            warnings.append(("unmapped_location", f"location {location!r} has no confirmed mapping; passed through as-is"))

    hire = termination = None
    if not value("hire_date"):
        errors.append(("missing_value", "hire_date is required"))
    else:
        try:
            hire = parse_date(value("hire_date"), spec.date_format)
        except ValueError:
            errors.append(("invalid_date", f"hire_date {value('hire_date')!r} does not match {spec.date_format}"))
    if value("termination_date"):
        try:
            termination = parse_date(value("termination_date"), spec.date_format)
        except ValueError:
            errors.append(("invalid_date", f"termination_date {value('termination_date')!r} does not match {spec.date_format}"))
    if hire and termination and termination < hire:
        errors.append(("termination_before_hire", "termination date precedes hire date"))
    if hire and hire > effective_date:
        warnings.append(("future_hire", f"hire date {hire.isoformat()} is after the effective date"))
    if termination and termination > effective_date:
        warnings.append(("pending_termination", f"termination {termination.isoformat()} is after the effective date; loaded as active"))

    salary = None
    raw_salary = value("base_salary")
    if spec.salary_pattern.fullmatch(raw_salary):
        salary = Decimal(re.sub(r"^\D+", "", raw_salary).replace(",", ""))
        low, high = spec.salary_range
        if not low <= salary <= high:
            warnings.append(("salary_out_of_range", f"salary {salary} is outside {low}-{high}; confirm it is annual base pay"))
    else:
        errors.append(("invalid_salary", f"salary {raw_salary or '<blank>'!r} is not a recognized amount"))

    manager_id = value("manager_id")
    if manager_id and manager_id == employee_id:
        errors.append(("self_manager", "employee cannot be their own manager"))

    if errors:
        return None, errors, warnings
    return {
        "employee_id": employee_id,
        "first_name": first_name,
        "last_name": last_name,
        "department": spec.departments[code],
        "job_title": value("job_title"),
        "manager_id": manager_id,
        "hire_date": hire.isoformat(),
        "termination_date": termination.isoformat() if termination else "",
        "base_salary": f"{salary:.2f}",
        "currency": spec.currency,
        "location": location,
        "employment_status": (
            "terminated" if termination and termination <= effective_date else "active"
        ),
    }, [], warnings


def find_manager_cycles(records: list[dict[str, str]]) -> set[str]:
    """Return employee IDs that sit on a manager cycle (A -> B -> A)."""
    manager_of = {r["employee_id"]: r["manager_id"] for r in records}
    state: dict[str, int] = {}  # 1 = on current path, 2 = done
    in_cycle: set[str] = set()
    for start in manager_of:
        path: list[str] = []
        node = start
        while node in manager_of and state.get(node) != 2:
            if state.get(node) == 1:
                in_cycle.update(path[path.index(node):])
                break
            state[node] = 1
            path.append(node)
            node = manager_of[node]
        for visited in path:
            state[visited] = 2
    return in_cycle


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def transform_csv(input_path: Path, config: dict, effective_date: date) -> Result:
    spec = load_spec(config)
    mapped_columns = list(spec.fields.values())

    groups: dict[str, list[tuple[int, dict[str, str], dict[str, str]]]] = defaultdict(list)
    errors: list[dict] = []
    warnings: list[dict] = []
    rejected: list[dict[str, str]] = []
    input_rows = rejected_rows = duplicate_rows = 0

    def reject(row_number: int, raw: dict[str, str], employee_id: str, problems: list[tuple[str, str]]) -> None:
        rejected.append({
            "source_row": str(row_number),
            "employee_id": employee_id,
            "error_codes": ";".join(code for code, _ in problems),
            "error_messages": "; ".join(message for _, message in problems),
            **{header: (raw.get(header) or "") for header in headers},
        })

    raw_bytes = input_path.read_bytes()
    with input_path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream, delimiter=spec.delimiter, skipinitialspace=True)
        if reader.fieldnames is None:
            raise ValueError("input CSV is empty")
        headers = [name.strip() for name in reader.fieldnames]
        reader.fieldnames = headers
        missing = set(mapped_columns) - set(headers)
        if missing:
            raise ValueError(f"missing source columns: {', '.join(sorted(missing))}")
        for row_number, raw in enumerate(reader, start=2):
            input_rows += 1
            if None in raw:
                problem = ("malformed_row", "row has more columns than the header")
                errors.append(issue([row_number], None, *problem))
                reject(row_number, raw, "", [problem])
                rejected_rows += 1
                continue
            row = {key: (value or "").strip() for key, value in raw.items()}
            employee_id = row[spec.fields["employee_id"]]
            if not employee_id:
                problem = ("missing_value", "employee_id is required")
                errors.append(issue([row_number], None, *problem))
                reject(row_number, raw, "", [problem])
                rejected_rows += 1
                continue
            groups[employee_id].append((row_number, row, raw))

    date_values = [
        row[spec.fields[concept]]
        for group in groups.values() for _, row, _ in group
        for concept in ("hire_date", "termination_date") if row[spec.fields[concept]]
    ]
    if dates_are_ambiguous(date_values, spec.date_format):
        warnings.append(issue([], None, "ambiguous_date_format", (
            f"every date also parses with day and month swapped; confirm the export uses {spec.date_format}"
        )))

    output: list[dict[str, str]] = []
    source_of: dict[str, tuple[int, dict[str, str], dict[str, str]]] = {}
    for employee_id, group in groups.items():
        first_number, first_row, first_raw = group[0]
        mapped = [first_row[column] for column in mapped_columns]
        if any([row[column] for column in mapped_columns] != mapped for _, row, _ in group[1:]):
            problem = ("conflicting_duplicate", "same employee ID has different source data")
            errors.append(issue([number for number, _, _ in group], employee_id, *problem))
            for number, _, raw in group:
                reject(number, raw, employee_id, [problem])
            rejected_rows += len(group)
            continue
        if len(group) > 1:
            duplicate_rows += len(group) - 1
            warnings.append(issue(
                [number for number, _, _ in group[1:]], employee_id,
                "identical_duplicate", "identical duplicate rows removed",
            ))
        record, row_errors, row_warnings = transform_record(first_row, spec, effective_date)
        warnings.extend(issue([first_number], employee_id, code, message) for code, message in row_warnings)
        if record is None:
            errors.extend(issue([first_number], employee_id, code, message) for code, message in row_errors)
            reject(first_number, first_raw, employee_id, row_errors)
            rejected_rows += 1
            continue
        output.append(record)
        source_of[employee_id] = group[0]

    cycle_ids = find_manager_cycles(output)
    if cycle_ids:
        problem = ("manager_cycle", "manager chain loops back to this employee")
        for record in output:
            employee_id = record["employee_id"]
            if employee_id in cycle_ids:
                number, _, raw = source_of[employee_id]
                errors.append(issue([number], employee_id, *problem))
                reject(number, raw, employee_id, [problem])
                rejected_rows += 1
        output = [record for record in output if record["employee_id"] not in cycle_ids]

    by_id = {record["employee_id"]: record for record in output}
    for record in output:
        employee_id, manager_id = record["employee_id"], record["manager_id"]
        number = source_of[employee_id][0]
        if not manager_id:
            continue
        if manager_id not in groups:
            warnings.append(issue([number], employee_id, "unknown_manager", (
                f"manager {manager_id} is absent from this export; confirm scope or ID"
            )))
        elif manager_id not in by_id:
            warnings.append(issue([number], employee_id, "manager_rejected", (
                f"manager {manager_id}'s own row was rejected; fix it so the reporting line loads"
            )))
        elif by_id[manager_id]["employment_status"] == "terminated" and record["employment_status"] == "active":
            warnings.append(issue([number], employee_id, "manager_terminated", (
                f"active employee reports to terminated manager {manager_id}"
            )))

    countries = sorted({
        country for record in output
        if (country := spec.locations.get(source_of[record["employee_id"]][1][spec.fields["location"]], {}).get("country"))
    })
    if len(countries) > 1:
        warnings.append(issue([], None, "multiple_countries", (
            f"export spans {', '.join(countries)} but config sets one currency ({spec.currency}); confirm pay currency per country"
        )))

    if input_rows != len(output) + duplicate_rows + rejected_rows:
        raise RuntimeError("row reconciliation failed: input != output + duplicates + rejected")

    report = {
        "script_version": SCRIPT_VERSION,
        "effective_date": effective_date.isoformat(),
        "input_sha256": sha256(raw_bytes),
        "config_sha256": sha256(json.dumps(config, sort_keys=True).encode("utf-8")),
        "config_version": config.get("version"),
        "counts": {
            "input_rows": input_rows,
            "output_rows": len(output),
            "identical_duplicate_rows": duplicate_rows,
            "rejected_rows": rejected_rows,
        },
        "summary_by_code": {
            "errors": dict(Counter(item["code"] for item in errors)),
            "warnings": dict(Counter(item["code"] for item in warnings)),
        },
        "errors": errors,
        "warnings": warnings,
    }
    return Result(rows=output, rejected=rejected, report=report, source_headers=headers)


def write_atomic(path: Path, write) -> None:
    """Write via a temp file in the same directory so readers never see a partial file."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as stream:
            write(stream)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def write_csv(path: Path, fieldnames, rows) -> None:
    def write(stream):
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    write_atomic(path, write)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="customer HRIS export")
    parser.add_argument("output", type=Path, help="CandorIQ ingest CSV")
    parser.add_argument("--config", required=True, type=Path, help="confirmed customer mapping JSON")
    parser.add_argument("--effective-date", required=True, help="export as-of date (YYYY-MM-DD)")
    parser.add_argument("--report", required=True, type=Path, help="exception report JSON")
    parser.add_argument("--rejected", type=Path, help="CSV of rejected source rows to send back to the customer (contains PII)")
    parser.add_argument("--allow-partial", action="store_true", help="write clean rows even when some rows are rejected (requires CS sign-off)")
    args = parser.parse_args(argv)

    paths = [args.input, args.output, args.report] + ([args.rejected] if args.rejected else [])
    if len({path.resolve() for path in paths}) != len(paths):
        parser.error("input, output, report, and rejected paths must all be different")
    for path in (args.output, args.rejected):
        if path and path.exists():
            parser.error(f"{path} already exists; choose a fresh path to avoid a stale file")
    try:
        effective_date = date.fromisoformat(args.effective_date)
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("config must be a JSON object")
        result = transform_csv(args.input, config, effective_date)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"error: {exc}\n")

    write_atomic(args.report, lambda stream: stream.write(json.dumps(result.report, indent=2) + "\n"))
    if args.rejected:
        write_csv(
            args.rejected,
            ["source_row", "employee_id", "error_codes", "error_messages", *result.source_headers],
            result.rejected,
        )
    errors = result.report["errors"]
    if errors and not args.allow_partial:
        print(f"{len(errors)} blocking issue(s); no ingest file written. See {args.report}", file=sys.stderr)
        return 1
    write_csv(args.output, OUTPUT_FIELDS, result.rows)
    if errors:
        print(f"Partial load: {result.report['counts']['rejected_rows']} row(s) rejected; see {args.report}", file=sys.stderr)
    print(f"Wrote {len(result.rows)} employees to {args.output}; report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
