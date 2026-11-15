"""Read a raw log (append-only, never written) and write a cleaned copy.

Usage:  python3 -I scripts/clean_raw.py data/raw/raw.csv data/interim/clean.csv

Idempotent: running it twice gives the same output. Raw rows are never
dropped or deduplicated; that happens at analysis time, using posting_key.

posting_key = employer + posting_id. Posting IDs are only unique within a
company, so the key needs both.

posting_id resolution order:
  1. the posting_id already in the raw row
  2. an ID pulled from the URL (see URL_ID_PATTERNS)
  3. a generated ID, a hash of employer, title and city
id_source records which of these was used.
"""
import csv
import hashlib
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DROP_COLUMNS = {"decision", "decision_reason"}
NEW_COLUMNS = ["posting_key", "id_source", "seniority_level", "job_family"]

# Tried in order; first match wins. Most specific first.
URL_ID_PATTERNS = [
    r"linkedin\.com/jobs/view/(\d+)",
    r"dice\.com/job-detail/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    r"lever\.co/[^/]+/([0-9a-f-]{36})",
    r"paylocity\.com/.*/Details/(\d+)",
    r"careercircle\.com/.*/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    r"[?&]jk=([0-9a-f]+)",  # Indeed
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",  # any UUID (Huntington, others)
    r"/details/(\d+)",  # Piper Companies
    r"/job-board/(\d+)$",  # Horizontal Talent
    r"/(\d+)/?$",  # generic trailing number (Stripe, Lyft/careerpuck); keep last
]

# Title keywords decide the family. Order matters: first match wins.
FAMILY_RULES = [
    (r"AI Risk", "AI risk governance"),
    (r"AIOps", "IT operations"),
    (r"Change Coordinator", "IT change management"),
    (r"Business Strategy Analyst", "Business analysis"),
    (r"Business Analyst", "Business analysis"),
    (r"Accounting|Payroll", "Accounting support"),
    (r"Financial Analyst", "Financial analysis"),
    (r"Data Scientist", "Data science"),
    (r"Data Engineer", "Data engineering"),
    (r"Data Quality|Data Integration|Data Governance", "Data quality"),
    (r"Data.*Analyst|Analyst.*Data", "Data analysis"),
    (r"Software Engineer", "Software engineering"),
]

# Title keywords decide seniority first; the grade column is a fallback.
SENIORITY_RULES = [
    (r"\bDirector\b", "Director"),
    (r"\bLead\b", "Lead"),
    (r"\bSenior\b|\bSr\.?\b", "Senior"),
]
# Grade values seen in the raw data, normalised. Unknown values are kept as-is.
GRADE_MAP = {"mid senior": "Mid-Senior"}


def clean_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return value  # already ISO or unrecognised; leave it alone


def clean_number(value: str, field: str, posting_key: str, decimals: int) -> str:
    """Salaries are whole dollars; hourly rates keep cents."""
    value = value.strip().replace("$", "").replace(",", "").strip()
    if not value:
        return ""
    try:
        number = float(value)
    except ValueError:
        raise SystemExit(f"Unparseable {field} for {posting_key}: {value!r}")
    return str(int(round(number))) if decimals == 0 else f"{number:.{decimals}f}"


def clean_title(value: str) -> str:
    return value.replace("–", "-").strip()  # en dash -> hyphen


def clean_url(value: str) -> str:
    """Drop query strings (utm tracking, Indeed's jk) from the stored URL."""
    value = value.strip()
    if not value:
        return ""
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def id_from_url(url: str) -> str:
    """Return the posting ID found in the raw URL, or '' if none matches."""
    for pattern in URL_ID_PATTERNS:
        match = re.search(pattern, url, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def generated_id(employer: str, title: str, city: str) -> str:
    basis = "|".join(s.strip().lower() for s in (employer, title, city))
    return "gen-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]


def seniority(title: str, grade: str) -> str:
    for pattern, label in SENIORITY_RULES:
        if re.search(pattern, title):
            return label
    if grade:
        return GRADE_MAP.get(grade.strip().lower(), grade.strip())
    return "Unspecified"


def job_family(title: str) -> str:
    for pattern, family in FAMILY_RULES:
        if re.search(pattern, title, flags=re.IGNORECASE):
            return family
    return "Other"


def main(src: Path, dst: Path) -> None:
    if src.resolve() == dst.resolve():
        raise SystemExit("Output must differ from input; the raw log is never written.")

    # utf-8-sig strips the byte-order mark that Excel writes at the start of the file
    with src.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "posting_id" not in reader.fieldnames:
            raise SystemExit("Missing posting_id column; refusing to continue.")
        original_cols = [c for c in reader.fieldnames if c not in DROP_COLUMNS]
        # skip fully blank rows (e.g. a trailing empty line from Excel)
        rows = [r for r in reader if any((v or "").strip() for v in r.values())]
    if not rows:
        raise SystemExit("No data rows found; refusing to continue.")

    out_cols = original_cols + [c for c in NEW_COLUMNS if c not in original_cols]
    cleaned = []
    for r in rows:
        employer = (r.get("employer") or "").strip()
        title = clean_title(r.get("title_as_posted") or "")
        city = (r.get("city") or "").strip()
        raw_url = (r.get("url") or "").strip()
        grade = (r.get("grade") or "").strip()

        pid = (r.get("posting_id") or "").strip()
        if pid:
            id_source = "raw"
        else:
            pid = id_from_url(raw_url)
            if pid:
                id_source = "url"
            else:
                pid = generated_id(employer, title, city)
                id_source = "generated"

        posting_key = f"{employer}|{pid}"
        row = {c: (r.get(c) or "").strip() for c in out_cols}
        row["posting_id"] = pid
        row["posting_key"] = posting_key
        row["id_source"] = id_source
        row["title_as_posted"] = title
        for col in ("salary_low", "salary_high"):
            row[col] = clean_number(r.get(col) or "", col, posting_key, decimals=0)
        for col in ("hourly_low", "hourly_high"):
            row[col] = clean_number(r.get(col) or "", col, posting_key, decimals=2)
        for col in ("email_date", "posted_date", "closing_date"):
            row[col] = clean_date(r.get(col) or "")
        row["url"] = clean_url(raw_url)
        # grade stays as entered in raw (filled by hand for federal/state jobs)
        row["seniority_level"] = seniority(title, grade)  # title rules, then raw grade
        row["job_family"] = job_family(title)
        cleaned.append(row)

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=out_cols)
        writer.writeheader()
        writer.writerows(cleaned)
    os.replace(tmp, dst)

    keys = [r["posting_key"] for r in cleaned]
    print(f"rows: {len(cleaned)}  unique posting_keys: {len(set(keys))}")
    for col in ("id_source", "seniority_level", "job_family"):
        counts = {}
        for r in cleaned:
            counts[r[col]] = counts.get(r[col], 0) + 1
        print(f"{col}: {counts}")
    if any(r["job_family"] == "Other" for r in cleaned):
        print("NOTE: some titles did not match a job_family rule; check them.")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RAW = PROJECT_ROOT / "data" / "raw" / "raw.csv"
DEFAULT_CLEAN = PROJECT_ROOT / "data" / "interim" / "clean.csv"

if __name__ == "__main__":
    # No arguments: read data/raw/raw.csv, write data/interim/clean.csv.
    # Optional: clean_raw.py <raw.csv> <clean.csv>
    if len(sys.argv) not in (1, 3):
        raise SystemExit("usage: clean_raw.py [raw.csv clean.csv]")
    src = Path(sys.argv[1]) if len(sys.argv) == 3 else DEFAULT_RAW
    dst = Path(sys.argv[2]) if len(sys.argv) == 3 else DEFAULT_CLEAN
    if not src.exists():
        raise SystemExit(f"Raw file not found: {src}")
    main(src, dst)
