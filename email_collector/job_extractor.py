
import re
import sqlite3



def extract_jobs_from_email(body):
    jobs = []
    seen_ids = set()

    lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip()
    ]

    ignored_prefixes = (
        "ta firma prowadzi",
        "aplikuj",
        "wyświetl ofertę pracy",
        "view job",
        "apply now",
    )

    for i, line in enumerate(lines):
        match = re.search(
            r"linkedin\.com/(?:comm/)?jobs/view/(\d+)",
            line
        )

        if not match:
            continue

        job_id = match.group(1)

        if job_id in seen_ids:
            continue

        # Find the three lines describing this job.
        previous_lines = []
        j = i - 1

        while j >= 0 and len(previous_lines) < 3:
            candidate = lines[j]

            if (
                candidate.lower().startswith(ignored_prefixes)
                or re.match(r"^\d+\s+kontakt\b", candidate, re.IGNORECASE)
            ):
                j -= 1
                continue

            previous_lines.append(candidate)
            j -= 1

        if len(previous_lines) != 3:
            continue

        title, company, location = reversed(previous_lines)

        # Ignore alert introductions and other obvious noise.
        if title.lower().startswith("twój alert"):
            continue

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

            inserted += cursor.rowcount

    conn.commit()

    total = conn.execute(
        "SELECT COUNT(*) FROM jobs"
    ).fetchone()[0]

    print(f"New jobs saved: {inserted}")
    print(f"Total unique jobs in database: {total}")

    conn.close()


if __name__ == "__main__":
    main()
