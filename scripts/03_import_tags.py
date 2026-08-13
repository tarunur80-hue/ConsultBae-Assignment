"""
Task 2 helper: import the Groq-generated skill_category tags (produced by
our n8n workflow) back into the persons table of our SQLite DB.

Usage:
  1. In n8n, open the Aggregate node's output, copy the JSON array under
     the "data" key, and save it as docs/n8n_tagged_results.json
     (it should look like: [{"person_id": "P0001", "name": "...",
      "skills": "...", "skill_category": "..."}, ...])
  2. Run: python scripts/03_import_tags.py
"""
import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "consultbae.db"
TAGS_PATH = Path(__file__).resolve().parent.parent / "docs" / "n8n_tagged_results.json"

with open(TAGS_PATH) as f:
    tagged = json.load(f)

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# add the column if it doesn't already exist
c.execute("PRAGMA table_info(persons)")
cols = [row[1] for row in c.fetchall()]
if "skill_category" not in cols:
    c.execute("ALTER TABLE persons ADD COLUMN skill_category TEXT")

updated = 0
for row in tagged:
    c.execute(
        "UPDATE persons SET skill_category = ? WHERE person_id = ?",
        (row["skill_category"], row["person_id"]),
    )
    updated += c.rowcount if c.rowcount else 0

conn.commit()
print(f"Tagged {len(tagged)} records from n8n; updated {updated} rows in persons table.")

# quick sanity check: show distribution of categories
print("\nCategory distribution:")
for cat, count in c.execute(
    "SELECT skill_category, COUNT(*) FROM persons WHERE skill_category IS NOT NULL GROUP BY skill_category"
):
    print(f"  {cat}: {count}")

conn.close()
