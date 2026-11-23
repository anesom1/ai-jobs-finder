
import re
import sqlite3


JOB_LINK_PATTERN = re.compile(
    r"linkedin\.com/(?:comm/)?jobs/view/(\d+)",
    re.IGNORECASE
)
DIVIDER_PATTERN = re.compile(r"^-{10,}$")
NOISE_PHRASES = (
    "this company is actively hiring",
    "apply with resume & profile",
    "apply with resume and profile",
    "easy apply",
    "be an early applicant",
)
IGNORED_PREFIXES = (
    "ta firma prowadzi",
    "aplikuj",
    "wyświetl ofertę pracy",
    "view job",
    "apply now",
    "your job alert",
    "twój alert",
)


def _is_job_metadata(line):
    normalized = re.sub(r"\s+", " ", line).strip().casefold()
    return (
        not normalized
        or DIVIDER_PATTERN.fullmatch(normalized)
        or normalized.startswith(IGNORED_PREFIXES)
        or any(phrase in normalized for phrase in NOISE_PHRASES)
        or re.match(r"^\d+\s+kontakt\b", normalized, re.IGNORECASE)
    )


def _job_details_before_link(lines, link_index):
    """Get title/company/location from this listing, not nearby UI text."""

    start = link_index - 1
    while start >= 0:
        line = lines[start]
        if (
            JOB_LINK_PATTERN.search(line)
            or DIVIDER_PATTERN.fullmatch(line)
            or line.casefold().startswith(("your job alert", "twój alert"))
        ):
            break
        start -= 1

    details = [
        line for line in lines[start + 1:link_index]
        if not _is_job_metadata(line)
    ]

    if len(details) < 2:
        return None

    title, company = details[-3:-1] if len(details) >= 3 else details[-2:]
    location = details[-1] if len(details) >= 3 else ""

    if not title or not company or _is_job_metadata(title) or _is_job_metadata(company):
        return None

    return title, company, location



def extract_jobs_from_email(body):
    jobs = []
    seen_ids = set()

    lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip()
    ]

    for i, line in enumerate(lines):
        match = JOB_LINK_PATTERN.search(line)

        if not match:
            continue

        job_id = match.group(1)

        if job_id in seen_ids:
            continue

        details = _job_details_before_link(lines, i)
        if not details:
            continue

        title, company, location = details

        jobs.append({
            "job_id": job_id,
            "title": title,
            "company": company,
            "location": location,
            "job_url": f"https://www.linkedin.com/jobs/view/{job_id}/",
            "source": "LinkedIn"
        })

        seen_ids.add(job_id)

    return jobs


def _has_malformed_job_fields(title, company, location):
    """Recognize obvious LinkedIn alert badges saved as job fields."""

    fields = (title, company, location)
    return (
        not str(title or "").strip()
        or not str(company or "").strip()
        or any(
            str(value or "").strip()
            and _is_job_metadata(str(value).strip())
            for value in fields
        )
    )


def create_jobs_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT,
            location TEXT,
            job_url TEXT,
            source TEXT,
            source_email_id TEXT,
            collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deleted_jobs (
            job_id TEXT PRIMARY KEY,
            deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()



def main():
    conn = sqlite3.connect("data/jobs_before_manual.db")

    create_jobs_table(conn)

    emails = conn.execute("""
        SELECT gmail_id, body
        FROM job_emails
        WHERE sender LIKE '%jobalerts-noreply%'
    """).fetchall()

    inserted = 0
    repaired = 0

    for gmail_id, body in emails:
        jobs = extract_jobs_from_email(body)

        for job in jobs:
            deleted = conn.execute(
                "SELECT 1 FROM deleted_jobs WHERE job_id = ?",
                (job["job_id"],)
            ).fetchone()
            if deleted:
                continue

            cursor = conn.execute("""
                INSERT OR IGNORE INTO jobs
                (job_id, title, company, location, job_url,
                 source, source_email_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                job["job_id"],
                job["title"],
                job["company"],
                job["location"],
                job["job_url"],
                job["source"],
                gmail_id
            ))

            if cursor.rowcount:
                inserted += cursor.rowcount
                continue

            existing = conn.execute("""
                SELECT title, company, location, source
                FROM jobs WHERE job_id = ?
            """, (job["job_id"],)).fetchone()
            if (
                existing
                and existing[3] == "LinkedIn"
                and _has_malformed_job_fields(*existing[:3])
            ):
                update_cursor = conn.execute("""
                    UPDATE jobs
                    SET title = ?, company = ?, location = ?
                    WHERE job_id = ? AND source = 'LinkedIn'
                """, (
                    job["title"],
                    job["company"],
                    job["location"],
                    job["job_id"]
                ))
                repaired += update_cursor.rowcount

    conn.commit()

    total = conn.execute(
        "SELECT COUNT(*) FROM jobs"
    ).fetchone()[0]

    print(f"New jobs saved: {inserted}")
    print(f"Malformed LinkedIn jobs repaired: {repaired}")
    print(f"Total unique jobs in database: {total}")

    conn.close()


if __name__ == "__main__":
    main()
