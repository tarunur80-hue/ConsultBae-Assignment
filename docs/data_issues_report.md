# Data Issues Report

Every data quality problem found across the 3 source files, and exactly what was done about it. Full detection/fix logic lives in `scripts/01_merge_pipeline.py`; a machine-readable version of this list is generated at `docs/data_issues_log.json` each time the pipeline runs.

## Cross-source problem: no single ID field

None of the 3 files share a common identifier.
- `source1_naukri_applicants.csv` has **email + phone**
- `source2_gig_workers.csv` has **email only** (no phone)
- `source3_cbnexus_contacts.csv` has **phone only** (no email)

**Fix:** used `source1` as a bridge table. Built a union-find (disjoint-set) structure that merges any two rows sharing a normalized phone OR a normalized email into one `person_id`. `source2` rows link to `source1` via email; `source3` rows link to `source1` via phone. Deliberately did **not** match on name — see the name-collision issue below for why that would have been wrong.

## source1_naukri_applicants.csv

| Issue | Detail | Fix |
|---|---|---|
| Inconsistent units | `Current CTC` column mixes raw annual rupees (e.g. `417964`) with values already in LPA (e.g. `4.2`) | Any value > 100 treated as raw rupees and divided by 100,000; everything normalized into one `ctc_lpa` float column |
| Inconsistent phone formats | Numbers appear as `+919000000254`, `9000000237`, `09000000287` | Stripped all non-digits, removed country code (`91`) or leading `0`, normalized to a clean 10-digit string |
| Inconsistent date formats | `Applied Date` uses at least 4 different formats: `24-07-2026`, `2026-08-08`, `07/13/2026`, `7 Jul 2026` | Tried multiple `strptime` formats in sequence until one parses; normalized to ISO `YYYY-MM-DD` |
| City name inconsistency | `GURGAON` / `gurugram` / `Gurgaon` (same city, different casing); `Bangalore` / `bangalore` / `Bengaluru` (renamed city); `Delhi` / `new delhi` / `Delhi NCR` (same city) | Built a synonym map to one canonical name per city; also trimmed stray trailing whitespace (e.g. `"Noida "`) |
| Duplicate row within source | "Rohit Verma" and "R. Verma" appear as two separate rows with identical email and phone — same person entered twice with an abbreviated name | Detected via matching (email, phone) pairs; kept the first occurrence, dropped the duplicate |
| Skill list formatting | Skills stored as a comma-separated string with mixed casing between sources | Lowercased and de-duplicated into a sorted list for consistent matching against source2's skill tags |

## source2_gig_workers.csv

| Issue | Detail | Fix |
|---|---|---|
| Completely blank row | One row in the file is entirely empty | Detected (all fields null) and dropped |
| Column-shifted / corrupted row | One row's values are rotated across columns — its `email_id` field contains `"react, javascript, mysql"` (clearly a skills list, not an email), while `worker_name` contains what looks like an email address | Validated `email_id` against an email-format regex; when it fails, checked whether rotating the row's fields recovers a record identical to an existing clean row for the same worker. It did — this is a corrupted duplicate export of "Isha Chopra", not a new person — so it was dropped rather than "fixed" and kept |
| Inconsistent units | `rate` column mixes hourly rates (`"1415/hr"`) and monthly rates (`"15k/month"`) with no way to compare them directly | Converted monthly rates to an estimated hourly rate assuming ~176 working hours/month (22 days × 8 hrs); all normalized into one `rate_hourly_inr` float column |
| Inconsistent casing (status) | `status` values appear as `Active` / `active` / `ACTIVE` / `Inactive` / `paused` | Lowercased and normalized to a consistent set (`active` / `inactive` / `paused`) |
| Inconsistent casing (email) | Several `email_id` values are in ALL CAPS or mixed case, which would break exact-match joins against source1's lowercase emails | Lowercased every email before using it as a join key |
| City name inconsistency | Same synonym/whitespace issues as source1 (`Bangalore` vs `Bengaluru`, trailing spaces, etc.) | Same canonical city map applied |

## source3_cbnexus_contacts.csv

| Issue | Detail | Fix |
|---|---|---|
| Duplicated header row mid-file | The column header line (`Name,Phone Number,City,Verified,Projects Completed`) is repeated partway through the file, as if two exports were concatenated without cleanup | Detected the repeated header line and stripped it before parsing, so it doesn't get read in as a fake data row |
| Inconsistent casing (Verified) | `Verified` column mixes `Y` / `N` / `yes` / `No` / `Yes` | Normalized to a proper boolean (`True`/`False`) |
| Inconsistent phone formats | Same issue as source1: `9000000268`, `+91-9000000131`, `919000000260` all appear | Same phone normalization applied (strip to clean 10-digit number) |
| ALL CAPS names | Several names appear fully capitalized (`RITU SHARMA`, `RAHUL MALHOTRA`) inconsistent with title-case entries elsewhere | Normalized to title case for display |
| **Name collision — two different people, same name (planted trap)** | "Arjun Mehta" appears twice with two **different** phone numbers (`9000000131` and `9000000272`) | This is not a duplicate — it's two distinct people who happen to share a name. Matching was deliberately restricted to phone/email only (never name), so these two correctly stayed as **separate** `person_id`s instead of being incorrectly merged into one |

## Result

- 41 valid rows from source1 (after dropping 1 in-source duplicate), 30 from source2 (after dropping 1 blank + 1 corrupted row), 30 from source3 (after removing the injected duplicate header)
- Entity resolution produced **60 unique persons**, of which **25 matched across 2 or more source files**
- Verified specifically that the planted name-collision case ("Arjun Mehta", also "Deepak Nair") stayed split into separate people, and that the planted duplicate-with-typo case ("Rohit Verma"/"R. Verma") correctly collapsed into one
