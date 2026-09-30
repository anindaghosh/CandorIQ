# Scenario 1: HRIS export transformation

I wrote a script that takes the customer's raw HRIS export and produces CandorIQ's employee ingest CSV. It also writes a JSON exception report, and optionally a CSV of the source rows it rejected, so CS can send those straight back to the customer.

The decision that shaped everything else: the code only knows CandorIQ's target schema and how to parse and validate. It knows nothing about any one customer. Column names, date format, department names, currency, all of that lives in a config file. The first transform for a customer is always the slowest, and I didn't want customer two to mean writing script two.

```sh
python scenario_1/transform_records.py raw.csv employees.csv \
  --config scenario_1/configs/example_customer.json \
  --effective-date 2026-09-29 --report exceptions.json \
  --rejected rejected_rows.csv
```

Two sample exports are in [samples/](samples/) so you can see both paths without writing your own:

- [samples/raw.csv](samples/raw.csv) is the prompt's data plus a few extra rows. It produces warnings only, so the ingest file gets written.
- [samples/raw_messy.csv](samples/raw_messy.csv) has one of each blocking error. It writes a report and no ingest file, which is the behavior I want for a bad export.

Both are pipe-delimited to match the prompt and work with the example config. Output paths must not already exist. The script needs Python 3.8 or later and uses only the standard library, so there is nothing to install.

The effective date is the export's as-of date, not the day I happen to run the script. `employment_status` depends on it, and I want a rerun next week to give the same answer as the first run.

## 1. Mapping and assumptions

| Source | Target | Rule |
| --- | --- | --- |
| `EmpID` | `employee_id` | Trim and keep as text, so leading zeros survive. |
| `Full_Name` | `first_name`, `last_name` | Split according to `name_format`. Names that don't match the format exactly (suffixes, extra commas, three-part `First Last` names) are rejected for review, not guessed. |
| `Dept_Code` | `department` | Look up in the confirmed `department_map` only. |
| `Title` | `job_title` | Trim outer whitespace and keep the wording. |
| `Mgr_EmpID` | `manager_id` | Trim and keep as text. Blank is allowed. |
| `Start_Date` | `hire_date` | Parse with `date_format` and output `YYYY-MM-DD`. |
| `Term_Date` | `termination_date`, `employment_status` | A blank becomes an empty cell (null on ingest). A date on or before the effective date means `terminated`. A later date stays `active` and is flagged as a pending termination. |
| `Annual_Comp` | `base_salary` | Strictly parsed and output as a two-decimal numeric value. Malformed amounts block the row. I'm assuming this column is base pay only; see below. |
| (config) | `currency` | Comes from the confirmed config. |
| `Loc` | `location` | Normalized through `location_map` when an entry exists; otherwise passed through with a warning. |

Five of these don't map cleanly, and this is where I made deliberate calls:

- **Names.** "Chen, David" is easy. "Chen, David, Jr." or "Mary Ann Smith" is not, because a wrong split quietly corrupts identity data. So anything that doesn't match the configured format exactly gets rejected for a human to look at.
- **Department codes.** `ENG-14` is a code, not a name. What it means is the customer's call, so the script only accepts a confirmed mapping. An unmapped code blocks the row.
- **Currency.** The export has no currency column, just a `$`, and a `$` alone doesn't identify a currency. I made `currency` a required config value with no default. If I defaulted to USD and the customer has Canadian staff, every one of those salaries would be wrong and the file would still look fine.
- **Location.** `Remote-CA` could mean California or Canada, and Canada would also change the currency. In the example config I left it unmapped on purpose so it shows up as a warning instead of being resolved by a guess.
- **Annual_Comp vs. base_salary.** The column is called "Annual Comp", which can include bonus or commission, while our field is base salary. I'm assuming it's base pay because the values look like round salaries, but that is an assumption, not something the file tells me. CS should confirm it with the customer before the first load, since loading total comp as base would overstate every salary and still pass every check. The `salary_out_of_range` warning helps a little, but it won't catch a uniform bonus uplift.

A few smaller assumptions about the file itself:

- **Wrapped rows in the prompt.** In the sample, `US-` / `Remote` and `Austin,` / `TX` are split across lines. I'm treating that as display wrapping, not real line breaks. If the real file is comma-delimited, a value like `Austin, TX` has to be quoted. An unquoted one shifts the columns, so the row is rejected as `malformed_row` instead of being guessed at. A pipe-delimited file, like the prompt's, has no such problem.
- **Repeated employee IDs.** I treat repeated IDs as duplicates. If the customer's system legitimately exports history (a rehire, a job change), different data under one ID will show up as a `conflicting_duplicate` error. That is the safe default, and CS should ask the customer which row is current before anything is dropped.
- **Scale.** About 5,000 rows is trivial, so the script reads the whole file into memory. A much larger file would need a streaming approach.

The config is where the customer-specific answers go. A new customer gets a new file, with no code change. See [configs/example_customer.json](configs/example_customer.json).

| Key | Required | Meaning |
| --- | --- | --- |
| `field_map` | yes | Maps target concepts to the customer's column headers. The name concepts depend on `name_format`. |
| `department_map` | yes | Confirmed mapping from department code to department name. Unmapped codes block the row. |
| `currency` | yes | Confirmed ISO 4217 code. `$` alone does not identify a currency. |
| `name_format` | no | `last_first` (default), `first_last`, or `separate_columns`. |
| `date_format` | no | strptime format of the **input** dates (default `%m/%d/%Y`). Output dates are always ISO `YYYY-MM-DD`. |
| `delimiter` | no | Default `,`. The sample in the prompt uses a pipe (`\|`). Header and value padding is trimmed. |
| `salary_symbol` | no | Optional prefix on amounts (default `$`). Use `""` for bare numbers. |
| `salary_range` | no | Plausible annual base pay (default `[10000, 2000000]`). Amounts outside the range are warnings. |
| `location_map` | no | Raw location mapped to `{"location", "country"}` or to a plain string. Unmapped values pass through with a warning. |
| `version` | no | Recorded in the report for audit. |

## 2. Transformation logic

The script goes in this order:

1. Read the file using the config's delimiter and trim the header and value padding.
2. Remove identical duplicate rows, and reject conflicting ones.
3. Parse and validate each remaining row: name, dates, salary, department.
4. Run the checks that only make sense across rows, like whether each manager exists in the export.
5. Reconcile the row counts, then write the output, the report, and the rejected file.

For the four things the prompt calls out:

- **The duplicate row for 10294.** The two rows are identical, so I keep one and log that I dropped the other. I compare on the mapped columns only, so an extra column like an export timestamp doesn't turn a harmless duplicate into a conflict. If the two rows had different data, I couldn't know which one is right, so that becomes a `conflicting_duplicate` error.
- **`"$118,500.00 "`.** Strip the outer whitespace, the `$` and the thousands separators, then parse strictly and output a two-decimal number. Anything malformed blocks the row instead of being coerced.
- **Dates.** Parse with the configured input format and always output `YYYY-MM-DD`. Strict parsing means an impossible date fails instead of rolling over into the next month.
- **`employment_status`.** A termination date on or before the effective date means `terminated`. A later one stays `active` and gets a `pending_termination` warning, because a scheduled exit is normal and I'd rather show it than hide it.

## 3. Validation: what I fix and what I flag

My rule of thumb: if there is only one right answer and I can prove it from the file, I fix it and log it. If fixing it means choosing between two plausible answers, I don't.

**Fixed mechanically, and logged:** outer whitespace, header padding, currency symbols and thousands separators, date reformatting, and identical duplicate rows (employee 10294).

**Blocking errors.** The row is rejected, and by default no ingest file is written.

| Code | Why it isn't auto-fixed |
| --- | --- |
| `conflicting_duplicate` | We can't know which version is correct. |
| `invalid_name` | Splitting a name wrongly corrupts identity data. |
| `unmapped_department` | Department meaning is the customer's decision. |
| `invalid_date`, `invalid_salary`, `missing_value` | Any fix would be a guess. |
| `termination_before_hire`, `self_manager`, `manager_cycle` | The org or timeline data contradicts itself. |
| `malformed_row` | The CSV structure is broken, so column alignment is unknown. |

Two choices here are worth explaining. First, all errors in a row are reported together, so the customer can fix everything in one round trip instead of finding a new problem each time. Second, the default is no output file when any row errors. A partial load that looks complete is worse than a load that didn't happen, because nobody goes looking for the missing rows.

**Warnings.** These rows load, but CS should confirm them with the customer:

- `unknown_manager`: the manager isn't in the export, which may mean the export is partial.
- `manager_rejected`: the manager's own row failed validation.
- `manager_terminated`
- `salary_out_of_range`: possibly hourly or monthly pay.
- `future_hire`
- `pending_termination`
- `unmapped_location`
- `multiple_countries`: one currency is covering several countries.
- `ambiguous_date_format`: every date would also parse with day and month swapped.

## 4. Handoff and checking the output

- The report reconciles every input row: `input_rows = output_rows + identical_duplicate_rows + rejected_rows`. A mismatch is a hard failure, so nothing can disappear without showing up.
- `summary_by_code` gives CS a one-glance triage, and each issue lists source row numbers and the employee ID.
- `input_sha256`, `config_sha256`, `config_version`, and `script_version` record exactly what produced a load, so I can answer "what did we load and how" later.
- `--rejected` writes the failing source rows with their original, untouched values and error codes, ready to send back to the customer. **This file contains PII (names and salaries). Share it only through approved channels.**
- `--allow-partial` loads the clean rows despite errors. Use it only with CS sign-off, for example to onboard 4,990 good rows while the customer fixes 10.
- Output files are written atomically (temp file, then rename), and existing output or rejected files are never overwritten, so a failed run can't leave a half-written file or clobber a previous load.

Run the checks with `python -m unittest discover -s scenario_1 -p 'test_*.py'`.

## 5. Reusability

The next customer needs a config file, not a script. The mapping, formats, and lookups are declarative, and the validation rules and report codes are shared, so CS sees the same exception vocabulary for every customer and doesn't have to relearn what a report means each time.

Next steps, in the order I'd do them: header auto-suggestion for `field_map`, a library of past customer configs, and loading `department_map` or `location_map` from a customer-reviewed sheet. I'd wait until a few customers have gone through before building anything bigger, since their configs will show which parts actually vary.