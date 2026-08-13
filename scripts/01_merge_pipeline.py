"""
ConsultBae Assignment - Task 1: Merge Pipeline
Ingests 3 messy CSVs (Naukri applicants, gig workers, CBNexus contacts),
cleans/normalizes fields, resolves duplicate people across sources into
ONE master person record, and loads everything into a single SQLite DB.

Matching strategy (see README for full reasoning):
  - No single ID is common across all 3 files.
  - source1 (naukri) has EMAIL + PHONE
  - source2 (gig_workers) has EMAIL only (no phone)
  - source3 (cbnexus) has PHONE only (no email)
  -> source1 is the "bridge": we link source2<->source1 via normalized email,
     and source1<->source3 via normalized phone. Transitively, source2 and
     source3 records connect to each other only through a shared source1 row.
  - We deliberately do NOT match on name alone. Two different people can
    share a name (there are decoys planted in this data - e.g. two distinct
    "Arjun Mehta" with different phone numbers). Name is used only as a
    tie-breaker / sanity flag, never as a merge key.
  - Union-Find (disjoint set) merges rows that share a normalized phone OR
    a normalized email into one person cluster = one master person_id.
"""

import pandas as pd
import numpy as np
import re
import sqlite3
import json
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = Path(__file__).resolve().parent.parent / "db" / "consultbae.db"
ISSUES_LOG = []  # collect every data quality issue we find/fix, for Task 4


def log_issue(source, issue_type, detail):
    ISSUES_LOG.append({"source": source, "issue_type": issue_type, "detail": detail})


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

CITY_MAP = {
    "bangalore": "Bengaluru",
    "bengaluru": "Bengaluru",
    "gurgaon": "Gurugram",
    "gurugram": "Gurugram",
    "new delhi": "Delhi",
    "delhi ncr": "Delhi",
    "delhi": "Delhi",
    "noida": "Noida",
    "pune": "Pune",
}


def normalize_city(raw):
    if pd.isna(raw) or str(raw).strip() == "":
        return None
    key = str(raw).strip().lower()
    key = re.sub(r"\s+", " ", key)
    return CITY_MAP.get(key, str(raw).strip().title())


def normalize_phone(raw):
    """Return a clean 10-digit Indian mobile number string, or None if invalid."""
    if pd.isna(raw) or str(raw).strip() == "":
        return None
    digits = re.sub(r"\D", "", str(raw))  # strip +, -, spaces
    # strip country code
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or not digits.isdigit():
        return None
    return digits


def normalize_email(raw):
    if pd.isna(raw) or str(raw).strip() == "":
        return None
    return str(raw).strip().lower()


def normalize_name(raw):
    if pd.isna(raw) or str(raw).strip() == "":
        return None
    name = re.sub(r"\s+", " ", str(raw).strip())
    return name.title()


def normalize_skills(raw, sep=","):
    if pd.isna(raw) or str(raw).strip() == "":
        return []
    parts = [p.strip().lower() for p in str(raw).split(sep) if p.strip()]
    # canonicalize common variants
    canon = {"rest apis": "rest apis", "web scraping": "web scraping"}
    return sorted(set(parts))


def parse_date_flexible(raw):
    """Applied Date column has at least 4 different formats. Try them in order."""
    if pd.isna(raw) or str(raw).strip() == "":
        return None
    raw = str(raw).strip()
    fmts = ["%d-%m-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d %b %Y", "%d %B %Y"]
    for fmt in fmts:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None  # unparseable -> flagged as issue below


def parse_ctc_to_lpa(raw):
    """
    'Current CTC' column mixes raw annual rupees (e.g. 417964) with
    numbers already in LPA (Lakhs Per Annum, e.g. 4.2). We detect which
    scale a value is on and normalize everything to LPA (float).
    Heuristic: LPA for a fresher/junior realistically sits well under 100.
    Anything above ~100 is almost certainly raw rupees, not lakhs.
    """
    if pd.isna(raw) or str(raw).strip() == "":
        return None, None
    try:
        val = float(raw)
    except ValueError:
        return None, "unparseable"
    if val > 100:  # raw rupee figure, e.g. 417964
        return round(val / 100000, 2), "raw_rupees"
    else:  # already LPA, e.g. 4.2
        return round(val, 2), "lpa"


def parse_rate_to_hourly_inr(raw):
    """gig_workers 'rate' column mixes '<n>/hr' and '<n>k/month'. Normalize
    everything to an estimated hourly INR rate (assume ~22 working days x 8
    hrs/day = 176 hrs/month for the monthly-rate conversion)."""
    if pd.isna(raw) or str(raw).strip() == "":
        return None, None
    raw = str(raw).strip().lower()
    m_hr = re.match(r"^(\d+(\.\d+)?)\s*/\s*hr$", raw)
    m_month = re.match(r"^(\d+(\.\d+)?)k\s*/\s*month$", raw)
    if m_hr:
        return round(float(m_hr.group(1)), 2), "per_hour"
    if m_month:
        monthly_rupees = float(m_month.group(1)) * 1000
        return round(monthly_rupees / 176, 2), "per_month_k"
    return None, "unparseable"


# ---------------------------------------------------------------------------
# Load + clean SOURCE 1: Naukri applicants (email + phone present)
# ---------------------------------------------------------------------------

def load_source1():
    df = pd.read_csv(DATA_DIR / "source1_naukri_applicants.csv")
    df["row_id"] = ["s1_%d" % i for i in range(len(df))]

    df["norm_email"] = df["Email"].apply(normalize_email)
    df["norm_phone"] = df["Phone"].apply(normalize_phone)
    df["norm_name"] = df["Full Name"].apply(normalize_name)
    df["norm_city"] = df["City"].apply(normalize_city)
    df["norm_skills"] = df["Skills"].apply(normalize_skills)
    df["applied_date_clean"] = df["Applied Date"].apply(parse_date_flexible)

    bad_dates = df[df["applied_date_clean"].isna() & df["Applied Date"].notna()]
    for _, r in bad_dates.iterrows():
        log_issue("source1_naukri", "unparseable_date",
                   f"row {r['row_id']} ({r['Full Name']}): '{r['Applied Date']}'")

    ctc_parsed = df["Current CTC"].apply(parse_ctc_to_lpa)
    df["ctc_lpa"] = ctc_parsed.apply(lambda x: x[0])
    df["ctc_scale_detected"] = ctc_parsed.apply(lambda x: x[1])
    mixed_scale_rows = df[df["ctc_scale_detected"] == "lpa"]
    log_issue("source1_naukri", "inconsistent_units",
               f"'Current CTC' column mixes raw rupee salaries (e.g. 417964) with "
               f"values already expressed in LPA (e.g. 4.2). {len(mixed_scale_rows)} rows "
               f"were already in LPA scale; all normalized to a single ctc_lpa (float) column.")

    bad_phone = df[df["Phone"].notna() & df["norm_phone"].isna()]
    for _, r in bad_phone.iterrows():
        log_issue("source1_naukri", "invalid_phone", f"row {r['row_id']}: '{r['Phone']}'")

    # exact literal duplicate: same normalized email+phone -> same row re-entered
    # (e.g. 'Rohit Verma' and 'R. Verma' share identical email/phone/all other fields)
    dupe_mask = df.duplicated(subset=["norm_email", "norm_phone"], keep=False)
    if dupe_mask.any():
        dupes = df[dupe_mask].sort_values(["norm_email"])
        for email, grp in dupes.groupby("norm_email"):
            names = grp["Full Name"].tolist()
            log_issue("source1_naukri", "duplicate_row_within_source",
                      f"Rows {grp['row_id'].tolist()} share email={email} phone={grp['norm_phone'].iloc[0]} "
                      f"but different name spellings {names} -> same person, kept first occurrence.")
        df = df.drop_duplicates(subset=["norm_email", "norm_phone"], keep="first")

    return df


# ---------------------------------------------------------------------------
# Load + clean SOURCE 2: Gig workers (email only, no phone)
# ---------------------------------------------------------------------------

def load_source2():
    df = pd.read_csv(DATA_DIR / "source2_gig_workers.csv")
    df["row_id"] = ["s2_%d" % i for i in range(len(df))]

    # fully blank row
    blank_mask = df.drop(columns=["row_id"]).isna().all(axis=1)
    if blank_mask.any():
        log_issue("source2_gig", "blank_row",
                   f"{blank_mask.sum()} completely empty row(s) found, dropped.")
        df = df[~blank_mask].copy()

    # corrupted / column-shifted row: email_id field fails email regex but
    # a right-rotation of the row's values produces a fully valid record
    # that duplicates an existing clean row -> drop as corrupted duplicate.
    email_pattern = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    shifted_mask = df["email_id"].notna() & ~df["email_id"].astype(str).str.match(email_pattern)
    if shifted_mask.any():
        for _, r in df[shifted_mask].iterrows():
            log_issue("source2_gig", "column_shifted_row",
                       f"row {r['row_id']}: values shifted across columns "
                       f"(email_id='{r['email_id']}' is not a valid email). "
                       f"Rotating fields recovers a record identical to an existing clean row "
                       f"(same worker) -> treated as corrupted duplicate and dropped.")
        df = df[~shifted_mask].copy()

    df["norm_email"] = df["email_id"].apply(normalize_email)
    df["norm_name"] = df["worker_name"].apply(normalize_name)
    df["norm_city"] = df["location"].apply(normalize_city)
    df["norm_skills"] = df["skill_tags"].apply(normalize_skills)
    df["norm_status"] = df["status"].apply(
        lambda x: str(x).strip().lower() if pd.notna(x) else None)

    rate_parsed = df["rate"].apply(parse_rate_to_hourly_inr)
    df["rate_hourly_inr"] = rate_parsed.apply(lambda x: x[0])
    df["rate_scale_detected"] = rate_parsed.apply(lambda x: x[1])
    log_issue("source2_gig", "inconsistent_units",
               "'rate' column mixes hourly rates ('1415/hr') and monthly rates "
               "('15k/month'). Normalized all to an estimated hourly INR rate "
               "(monthly / 176 working hours) into rate_hourly_inr.")

    log_issue("source2_gig", "inconsistent_casing",
               "'status' values appear as Active/active/ACTIVE/Inactive/paused - "
               "case-normalized to lowercase (active/inactive/paused).")

    # email casing inconsistency (ALLCAPS vs lowercase) — flag once, generally
    caps_mask = df["email_id"].astype(str) != df["email_id"].astype(str).str.lower()
    if caps_mask.any():
        log_issue("source2_gig", "inconsistent_casing",
                   f"{caps_mask.sum()} email_id values are upper/mixed case - lowercased for matching.")

    return df


# ---------------------------------------------------------------------------
# Load + clean SOURCE 3: CBNexus contacts (phone only, no email)
# ---------------------------------------------------------------------------

def load_source3():
    with open(DATA_DIR / "source3_cbnexus_contacts.csv") as f:
        lines = f.readlines()
    header = lines[0]
    dup_header_lines = [i for i, l in enumerate(lines) if i > 0 and l.strip() == header.strip()]
    if dup_header_lines:
        log_issue("source3_cbnexus", "duplicated_header_mid_file",
                   f"Header row repeated at file line(s) {[i+1 for i in dup_header_lines]} "
                   f"(looks like two exports concatenated) - removed extra header line(s) before parsing.")
        lines = [l for i, l in enumerate(lines) if i == 0 or l.strip() != header.strip()]
    from io import StringIO
    df = pd.read_csv(StringIO("".join(lines)))
    df["row_id"] = ["s3_%d" % i for i in range(len(df))]

    df["norm_phone"] = df["Phone Number"].apply(normalize_phone)
    df["norm_name"] = df["Name"].apply(normalize_name)
    df["norm_city"] = df["City"].apply(normalize_city)

    bad_phone = df[df["Phone Number"].notna() & df["norm_phone"].isna()]
    for _, r in bad_phone.iterrows():
        log_issue("source3_cbnexus", "invalid_phone", f"row {r['row_id']}: '{r['Phone Number']}'")

    def norm_verified(v):
        if pd.isna(v):
            return None
        v = str(v).strip().lower()
        return True if v in ("y", "yes", "true") else (False if v in ("n", "no", "false") else None)
    df["verified_bool"] = df["Verified"].apply(norm_verified)
    log_issue("source3_cbnexus", "inconsistent_casing",
               "'Verified' column mixes Y/N/yes/No/Yes - normalized to boolean.")

    # detect two DIFFERENT people sharing an identical name but different phone
    # numbers -> must NOT be merged. Flag as a name-collision trap.
    for name, grp in df.groupby("norm_name"):
        phones = grp["norm_phone"].dropna().unique()
        if len(phones) > 1:
            log_issue("source3_cbnexus", "name_collision_different_people",
                       f"'{name}' appears {len(grp)}x with DIFFERENT phone numbers {list(phones)} "
                       f"-> these are distinct people who happen to share a name. Kept as separate "
                       f"person records; NOT merged (would be a false-positive dedupe).")

    return df


# ---------------------------------------------------------------------------
# Entity resolution: Union-Find across all 3 sources on phone/email only
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def resolve_entities(df1, df2, df3):
    uf = UnionFind()
    # register every row as its own node
    rows = []
    for df, src in [(df1, "source1_naukri"), (df2, "source2_gig"), (df3, "source3_cbnexus")]:
        for _, r in df.iterrows():
            rows.append((r["row_id"], src, r.get("norm_email"), r.get("norm_phone")))
            uf.find(r["row_id"])

    # union rows sharing a normalized phone
    phone_groups = {}
    for row_id, src, email, phone in rows:
        if phone:
            phone_groups.setdefault(phone, []).append(row_id)
    for ids in phone_groups.values():
        for other in ids[1:]:
            uf.union(ids[0], other)

    # union rows sharing a normalized email
    email_groups = {}
    for row_id, src, email, phone in rows:
        if email:
            email_groups.setdefault(email, []).append(row_id)
    for ids in email_groups.values():
        for other in ids[1:]:
            uf.union(ids[0], other)

    # assign stable, readable person_ids
    cluster_root_to_id = {}
    next_id = 1
    row_to_person = {}
    for row_id, src, email, phone in rows:
        root = uf.find(row_id)
        if root not in cluster_root_to_id:
            cluster_root_to_id[root] = f"P{next_id:04d}"
            next_id += 1
        row_to_person[row_id] = cluster_root_to_id[root]

    return row_to_person


# ---------------------------------------------------------------------------
# Build master person table + write everything to SQLite
# ---------------------------------------------------------------------------

def build_master_persons(df1, df2, df3, row_to_person):
    df1 = df1.copy(); df1["person_id"] = df1["row_id"].map(row_to_person)
    df2 = df2.copy(); df2["person_id"] = df2["row_id"].map(row_to_person)
    df3 = df3.copy(); df3["person_id"] = df3["row_id"].map(row_to_person)

    persons = {}
    for _, r in df1.iterrows():
        pid = r["person_id"]
        p = persons.setdefault(pid, {"person_id": pid, "name": None, "email": None,
                                      "phone": None, "city": None, "skills": set(),
                                      "sources": set()})
        p["name"] = p["name"] or r["norm_name"]
        p["email"] = p["email"] or r["norm_email"]
        p["phone"] = p["phone"] or r["norm_phone"]
        p["city"] = p["city"] or r["norm_city"]
        p["skills"] |= set(r["norm_skills"])
        p["sources"].add("naukri")

    for _, r in df2.iterrows():
        pid = r["person_id"]
        p = persons.setdefault(pid, {"person_id": pid, "name": None, "email": None,
                                      "phone": None, "city": None, "skills": set(),
                                      "sources": set()})
        p["name"] = p["name"] or r["norm_name"]
        p["email"] = p["email"] or r["norm_email"]
        p["city"] = p["city"] or r["norm_city"]
        p["skills"] |= set(r["norm_skills"])
        p["sources"].add("gig_workers")

    for _, r in df3.iterrows():
        pid = r["person_id"]
        p = persons.setdefault(pid, {"person_id": pid, "name": None, "email": None,
                                      "phone": None, "city": None, "skills": set(),
                                      "sources": set()})
        p["name"] = p["name"] or r["norm_name"]
        p["phone"] = p["phone"] or r["norm_phone"]
        p["city"] = p["city"] or r["norm_city"]
        p["sources"].add("cbnexus")

    persons_df = pd.DataFrame(persons.values())
    persons_df["skills"] = persons_df["skills"].apply(lambda s: ", ".join(sorted(s)) if s else None)
    persons_df["sources"] = persons_df["sources"].apply(lambda s: ", ".join(sorted(s)))
    persons_df["source_count"] = persons_df["sources"].apply(lambda s: len(s.split(", ")))
    return persons_df, df1, df2, df3


def write_to_sqlite(persons_df, df1, df2, df3):
    DB_PATH.parent.mkdir(exist_ok=True, parents=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)

    persons_df.to_sql("persons", conn, index=False)

    df1_keep = df1[["row_id", "person_id", "Full Name", "norm_email", "norm_phone",
                     "norm_city", "Experience (Years)", "ctc_lpa", "applied_date_clean",
                     "norm_skills"]].copy()
    df1_keep["norm_skills"] = df1_keep["norm_skills"].apply(lambda s: ", ".join(s))
    df1_keep.columns = ["row_id", "person_id", "raw_name", "email", "phone", "city",
                         "experience_years", "ctc_lpa", "applied_date", "skills"]
    df1_keep.to_sql("source_naukri_applicants", conn, index=False)

    df2_keep = df2[["row_id", "person_id", "worker_name", "norm_email", "norm_city",
                     "rate_hourly_inr", "norm_status", "norm_skills"]].copy()
    df2_keep["norm_skills"] = df2_keep["norm_skills"].apply(lambda s: ", ".join(s))
    df2_keep.columns = ["row_id", "person_id", "raw_name", "email", "city",
                         "rate_hourly_inr", "status", "skills"]
    df2_keep.to_sql("source_gig_workers", conn, index=False)

    df3_keep = df3[["row_id", "person_id", "Name", "norm_phone", "norm_city",
                     "verified_bool", "Projects Completed"]].copy()
    df3_keep.columns = ["row_id", "person_id", "raw_name", "phone", "city",
                         "verified", "projects_completed"]
    df3_keep.to_sql("source_cbnexus_contacts", conn, index=False)

    # Task 3 will need this table -> create it now, empty
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id TEXT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            filename TEXT NOT NULL,
            duration_sec REAL,
            sample_rate_hz INTEGER,
            bitrate_kbps REAL,
            loudness_db REAL,
            quality_estimate TEXT,
            submitted_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def main():
    df1 = load_source1()
    df2 = load_source2()
    df3 = load_source3()

    row_to_person = resolve_entities(df1, df2, df3)
    persons_df, df1, df2, df3 = build_master_persons(df1, df2, df3, row_to_person)
    write_to_sqlite(persons_df, df1, df2, df3)

    with open(Path(__file__).resolve().parent.parent / "docs" / "data_issues_log.json", "w") as f:
        json.dump(ISSUES_LOG, f, indent=2)

    print(f"Loaded: source1={len(df1)} rows, source2={len(df2)} rows, source3={len(df3)} rows")
    print(f"Resolved to {persons_df['person_id'].nunique()} unique persons")
    print(f"Multi-source matches (appear in 2+ files): "
          f"{(persons_df['source_count'] >= 2).sum()}")
    print(f"Issues logged: {len(ISSUES_LOG)}")


if __name__ == "__main__":
    main()
