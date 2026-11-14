"""Read data/raw.csv (append-only, never written) and write data/clean.csv.

Idempotent: running it twice gives the same output. Raw rows are never
dropped or deduplicated; that happens at analysis time.

Usage:  python3 -I scripts/clean_raw.py data/raw.csv data/clean.csv
"""
import csv
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DROP_COLUMNS = {"decision", "decision_reason"}
NEW_COLUMNS = ["seniority_level", "job_family"]

FAMILY_RULES = [
    (r"AI Risk", "AI risk governance"),
    (r"AIOps", "IT operations"),
    (r"Change Coordinator", "IT change management"),
    (r"Business Strategy Analyst", "Business analysis"),
    (r"Accounting", "Accounting support"),
]


def clean_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return value  # already ISO or unrecognised; leave it alone


def clean_money(value: str, field: str, posting_id: str) -> str:
    value = value.strip().replace("$", "").replace(",", "").strip()
    if not value:
        return ""
    try:
        return str(int(round(float(value))))
    except ValueError:
        raise SystemExit(f"Unparseable {field} for {posting_id}: {value!r}")


def clean_title(value: str) -> str:
    return value.replace("–", "-").strip()  # en dash -> hyphen


def clean_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def seniority(title: str) -> str:
    if re.search(r"\bDirector\b", title):
        return "Director"
    if re.search(r"\bLead\b", title):
        return "Lead"
    if re.search(r"\bSenior\b|\bSr\.?\b", title):
        return "Senior"
    return "Unspecified"


def job_family(title: str) -> str:
    for pattern, family in FAMILY_RULES:
        if re.search(pattern, title):
            return family
    return "Other"


def main(src: Path, dst: Path) -> None:
    if src.resolve() == dst.resolve():
        raise SystemExit("Output must differ from input; raw.csv is never written.")
    # utf-8-sig strips the byte-order mark that Excel writes at the start of the file
    with src.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "posting_id" not in reader.fieldnames:
            raise SystemExit("Missing posting_id column; refusing to overwrite the file.")
        original_cols = [c for c in reader.fieldnames if c not in DROP_COLUMNS]
        rows = [r for r in reader if (r.get("posting_id") or "").strip()]
    if not rows:
        raise SystemExit("No data rows found; refusing to overwrite the file.")

    out_cols = original_cols + [c for c in NEW_COLUMNS if c not in original_cols]
    cleaned = []
    for r in rows:
        pid = r["posting_id"].strip()
        title = clean_title(r.get("title_as_posted", ""))
        row = {c: (r.get(c) or "").strip() for c in out_cols}
        row["posting_id"] = pid
        row["title_as_posted"] = title
        for col in ("salary_low", "salary_high"):
            row[col] = clean_money(r.get(col, ""), col, pid)
        for col in ("hourly_low", "hourly_high"):
            row[col] = clean_money(r.get(col, ""), col, pid)
        for col in ("email_date", "posted_date", "closing_date"):
            row[col] = clean_date(r.get(col, ""))
        row["url"] = clean_url(r.get("url", ""))
        row["seniority_level"] = seniority(title)
        row["job_family"] = job_family(title)
        cleaned.append(row)

    tmp = dst.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=out_cols)
        writer.writeheader()
        writer.writerows(cleaned)
    os.replace(tmp, dst)

    ids = [r["posting_id"] for r in cleaned]
    print(f"rows: {len(cleaned)}  unique posting_ids: {len(set(ids))}")
    for col in ("seniority_level", "job_family"):
        counts = {}
        for r in cleaned:
            counts[r[col]] = counts.get(r[col], 0) + 1
        print(f"{col}: {counts}")
    if "Other" in {r["job_family"] for r in cleaned}:
        print("WARNING: some titles did not match a job_family rule; check them.")


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit("usage: clean_raw.py <raw.csv> [clean.csv]")
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) == 3 else src.with_name("clean.csv")
    main(src, dst)
