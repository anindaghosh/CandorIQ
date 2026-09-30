"""Focused checks for the representative HRIS export and blocking exceptions."""

import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from transform_records import main, transform_csv


EXAMPLE_CONFIG = Path(__file__).parent / "configs" / "example_customer.json"
HEADERS = [
    "EmpID", "Full_Name", "Dept_Code", "Title", "Mgr_EmpID",
    "Start_Date", "Term_Date", "Annual_Comp", "Loc",
]
FIELD_MAP = {
    "employee_id": "EmpID", "full_name": "Full_Name", "department_code": "Dept_Code",
    "job_title": "Title", "manager_id": "Mgr_EmpID", "hire_date": "Start_Date",
    "termination_date": "Term_Date", "base_salary": "Annual_Comp", "location": "Loc",
}
CONFIG = {
    "currency": "USD",  # Customer confirmation is represented by config input.
    "department_map": {"ENG-14": "Engineering", "SLS-02": "Sales"},
    "field_map": FIELD_MAP,
}
SAMPLE = [
    ["10293", "Alvarez, Maria", "ENG-14", "Sr. Software Eng", "10201", "03/14/2022", "", "$142,000.00", "US-Remote"],
    ["10294", "Chen, David", "ENG-14", "Software Eng II", "10201", "07/01/2023", "", "$118,500.00 ", "Austin, TX"],
    ["10294", "Chen, David", "ENG-14", "Software Eng II", "10201", "07/01/2023", "", "$118,500.00 ", "Austin, TX"],
    ["10301", "Osei, Kwame", "SLS-02", "AE", "10250", "11/28/2021", "09/05/2026", "$95,000.00", "Remote-CA"],
]
# The export exactly as shown in the scenario prompt: pipe-delimited, padded, quoted salary.
PIPE_SAMPLE = """EmpID | Full_Name | Dept_Code | Title | Mgr_EmpID | Start_Date | Term_Date | Annual_Comp | Loc
10293 | Alvarez, Maria | ENG-14 | Sr. Software Eng | 10201 | 03/14/2022 | | "$142,000.00" | US-Remote
10294 | Chen, David | ENG-14 | Software Eng II | 10201 | 07/01/2023 | | "$118,500.00 " | Austin, TX
10294 | Chen, David | ENG-14 | Software Eng II | 10201 | 07/01/2023 | | "$118,500.00 " | Austin, TX
10301 | Osei, Kwame | SLS-02 | AE | 10250 | 11/28/2021 | 09/05/2026 | "$95,000.00" | Remote-CA
"""


def codes(items):
    return [item["code"] for item in items]


class TransformTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "source.csv"

    def write_rows(self, rows, headers=HEADERS):
        with self.path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            writer.writerows(rows)

    def run_cli(self, config, *extra):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config))
        self.output, self.report = self.root / "output.csv", self.root / "report.json"
        return main([
            str(self.path), str(self.output), "--config", str(config_path),
            "--effective-date", "2026-09-29", "--report", str(self.report), *extra,
        ])

    def test_representative_export(self):
        self.write_rows(SAMPLE)
        result = transform_csv(self.path, CONFIG, date(2026, 9, 29))
        rows, report = result.rows, result.report
        self.assertEqual(report["counts"], {
            "input_rows": 4, "output_rows": 3,
            "identical_duplicate_rows": 1, "rejected_rows": 0,
        })
        self.assertFalse(report["errors"])
        self.assertEqual([row["employee_id"] for row in rows], ["10293", "10294", "10301"])
        self.assertEqual((rows[0]["first_name"], rows[0]["last_name"]), ("Maria", "Alvarez"))
        self.assertEqual(rows[0]["hire_date"], "2022-03-14")
        self.assertEqual(rows[0]["termination_date"], "")
        self.assertEqual(rows[1]["base_salary"], "118500.00")
        self.assertEqual(rows[2]["termination_date"], "2026-09-05")
        self.assertEqual(rows[2]["employment_status"], "terminated")
        self.assertIn("identical_duplicate", codes(report["warnings"]))
        self.assertIn("unknown_manager", codes(report["warnings"]))
        self.assertEqual(report["summary_by_code"]["warnings"]["unknown_manager"], 3)
        self.assertEqual(len(report["input_sha256"]), 64)

    def test_prompt_export_with_example_config(self):
        self.path.write_text(PIPE_SAMPLE, encoding="utf-8")
        config = json.loads(EXAMPLE_CONFIG.read_text())
        result = transform_csv(self.path, config, date(2026, 9, 29))
        self.assertFalse(result.report["errors"])
        self.assertEqual(len(result.rows), 3)
        self.assertEqual(result.rows[1]["base_salary"], "118500.00")
        self.assertEqual(result.rows[1]["location"], "Austin, TX, US")
        self.assertEqual(result.rows[2]["employment_status"], "terminated")
        # Remote-CA could be California or Canada: flagged, not guessed.
        self.assertIn("unmapped_location", codes(result.report["warnings"]))
        self.assertEqual(result.report["config_version"], "2026-09-29.1")

    def test_future_termination_is_active_and_flagged(self):
        self.write_rows([SAMPLE[-1]])
        result = transform_csv(self.path, CONFIG, date(2026, 9, 1))
        self.assertEqual(result.rows[0]["employment_status"], "active")
        self.assertIn("pending_termination", codes(result.report["warnings"]))

    def test_conflicting_duplicate_blocks_both_rows(self):
        changed = SAMPLE[1].copy()
        changed[3] = "Manager"
        self.write_rows([SAMPLE[1], changed])
        result = transform_csv(self.path, CONFIG, date(2026, 9, 29))
        self.assertEqual(result.rows, [])
        self.assertEqual(result.report["counts"]["rejected_rows"], 2)
        self.assertEqual(codes(result.report["errors"]), ["conflicting_duplicate"])

    def test_unmapped_extra_column_does_not_create_conflict(self):
        first, second = SAMPLE[1] + ["2026-01-01"], SAMPLE[1] + ["2026-02-01"]
        self.write_rows([first, second], HEADERS + ["Exported_At"])
        result = transform_csv(self.path, CONFIG, date(2026, 9, 29))
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.report["counts"]["identical_duplicate_rows"], 1)

    def test_all_errors_in_a_row_are_reported_together(self):
        bad = SAMPLE[0].copy()
        bad[1], bad[2], bad[5], bad[7] = "Maria Alvarez", "UNK-1", "02/30/2022", "$14,2.00"
        self.write_rows([bad])
        result = transform_csv(self.path, CONFIG, date(2026, 9, 29))
        self.assertEqual(result.rows, [])
        self.assertEqual(result.report["counts"]["rejected_rows"], 1)
        self.assertEqual(
            sorted(codes(result.report["errors"])),
            ["invalid_date", "invalid_name", "invalid_salary", "unmapped_department"],
        )
        self.assertEqual(result.rejected[0]["error_codes"].count(";"), 3)

    def test_rejected_manager_is_not_reported_as_absent(self):
        manager = ["10201", "Lee, Ana", "ENG-14", "Director", "", "01/01/2020", "", "not money", "US-Remote"]
        self.write_rows([manager, SAMPLE[0]])
        result = transform_csv(self.path, CONFIG, date(2026, 9, 29))
        self.assertIn("manager_rejected", codes(result.report["warnings"]))
        self.assertNotIn("unknown_manager", codes(result.report["warnings"]))

    def test_manager_cycle_is_blocking(self):
        a = ["1", "A, Ann", "ENG-14", "Eng", "2", "01/01/2020", "", "$100,000.00", "US-Remote"]
        b = ["2", "B, Bob", "ENG-14", "Eng", "1", "01/01/2020", "", "$100,000.00", "US-Remote"]
        c = ["3", "C, Cat", "ENG-14", "Eng", "1", "01/01/2020", "", "$100,000.00", "US-Remote"]
        self.write_rows([a, b, c])
        result = transform_csv(self.path, CONFIG, date(2026, 9, 29))
        self.assertEqual([row["employee_id"] for row in result.rows], ["3"])
        self.assertEqual(codes(result.report["errors"]), ["manager_cycle", "manager_cycle"])
        self.assertIn("manager_rejected", codes(result.report["warnings"]))

    def test_plausibility_warnings(self):
        hourly = SAMPLE[0].copy()
        hourly[7] = "$45.00"
        future = SAMPLE[3].copy()
        future[5], future[6] = "12/01/2026", ""
        self.write_rows([hourly, future])
        result = transform_csv(self.path, CONFIG, date(2026, 9, 29))
        self.assertEqual(len(result.rows), 2)
        self.assertIn("salary_out_of_range", codes(result.report["warnings"]))
        self.assertIn("future_hire", codes(result.report["warnings"]))

    def test_ambiguous_day_month_is_flagged(self):
        row = SAMPLE[0].copy()
        row[5] = "03/04/2022"
        self.write_rows([row])
        result = transform_csv(self.path, CONFIG, date(2026, 9, 29))
        self.assertIn("ambiguous_date_format", codes(result.report["warnings"]))

    def test_configurable_formats_output_iso_dates(self):
        headers = ["id", "first", "last", "dept", "title", "mgr", "start", "end", "pay", "site"]
        self.write_rows([["7", "Maria", "Alvarez", "E", "Eng", "", "14.03.2022", "", "142000", "Berlin"]], headers)
        config = {
            "currency": "EUR", "department_map": {"E": "Engineering"},
            "name_format": "separate_columns", "date_format": "%d.%m.%Y", "salary_symbol": "",
            "field_map": {
                "employee_id": "id", "first_name": "first", "last_name": "last",
                "department_code": "dept", "job_title": "title", "manager_id": "mgr",
                "hire_date": "start", "termination_date": "end", "base_salary": "pay", "location": "site",
            },
        }
        result = transform_csv(self.path, config, date(2026, 9, 29))
        self.assertFalse(result.report["errors"])
        self.assertEqual(result.rows[0]["hire_date"], "2022-03-14")
        self.assertEqual(result.rows[0]["base_salary"], "142000.00")

    def test_multiple_countries_with_one_currency_is_flagged(self):
        toronto = SAMPLE[3].copy()
        toronto[8] = "Toronto"
        self.write_rows([SAMPLE[0], toronto])
        config = CONFIG | {"location_map": {
            "US-Remote": {"location": "Remote - US", "country": "US"},
            "Toronto": {"location": "Toronto, ON", "country": "CA"},
        }}
        result = transform_csv(self.path, config, date(2026, 9, 29))
        self.assertIn("multiple_countries", codes(result.report["warnings"]))

    def test_cli_blocks_output_and_writes_rejected_rows(self):
        self.write_rows([SAMPLE[0], SAMPLE[1]])
        bad_config = CONFIG | {"department_map": {"OTHER": "Other"}}
        rejected = self.root / "rejected.csv"
        self.assertEqual(self.run_cli(bad_config, "--rejected", str(rejected)), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(json.loads(self.report.read_text())["counts"]["rejected_rows"], 2)
        with rejected.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row["source_row"] for row in rows], ["2", "3"])
        self.assertEqual(rows[1]["Annual_Comp"], "$118,500.00 ")  # raw value, untouched
        self.assertEqual(rows[0]["error_codes"], "unmapped_department")

    def test_cli_allow_partial_writes_clean_rows(self):
        bad = SAMPLE[0].copy()
        bad[2] = "UNK-1"
        self.write_rows([bad, SAMPLE[1]])
        self.assertEqual(self.run_cli(CONFIG, "--allow-partial"), 0)
        with self.output.open(newline="", encoding="utf-8") as stream:
            self.assertEqual([row["employee_id"] for row in csv.DictReader(stream)], ["10294"])

    def test_cli_writes_ingest_csv_for_sample(self):
        self.write_rows(SAMPLE)
        self.assertEqual(self.run_cli(CONFIG), 0)
        with self.output.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["base_salary"], "118500.00")
        self.assertEqual(rows[2]["employment_status"], "terminated")
        self.assertEqual(json.loads(self.report.read_text())["counts"]["identical_duplicate_rows"], 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["config.json", "output.csv", "report.json", "source.csv"])


if __name__ == "__main__":
    unittest.main()
