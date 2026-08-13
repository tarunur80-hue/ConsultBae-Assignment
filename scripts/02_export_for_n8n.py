"""
Task 2 helper: export persons + skills from the SQLite DB to a CSV.
n8n Cloud runs on Google's servers and cannot read our local SQLite file
directly, so we export the slice of data the automation needs, and later
re-import the tagged results back into the DB (see 03_import_tags.py).
"""
import sqlite3
import csv
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "consultbae.db"
OUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "persons_skills_export.csv"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
rows = c.execute(
    "SELECT person_id, name, skills FROM persons WHERE skills IS NOT NULL AND skills != ''"
).fetchall()

with open(OUT_PATH, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["person_id", "name", "skills"])
    w.writerows(rows)

print(f"Exported {len(rows)} people with skills -> {OUT_PATH}")
conn.close()
