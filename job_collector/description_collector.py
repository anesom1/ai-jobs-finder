
import html
import sqlite3
import requests
import time
import re

from bs4 import BeautifulSoup


DB_PATH = "data/jobs_before_manual.db"
BATCH_SIZE = 25
REQUEST_DELAY = 2
MIN_DESCRIPTION_LENGTH = 300

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobAssistant/1.0)"
}


def get_jobs(conn):
    conn.row_factory = sqlite3.Row

    return conn.execute("""
        SELECT job_id, title, company, job_url
        FROM jobs
        WHERE description_status IN ('pending', 'unverified')
        ORDER BY collected_at DESC
    """).fetchall()


def normalized_company_text(value):
    """Normalize company text from LinkedIn HTML before comparing it."""

    return re.sub(
        r"\s+",
        " ",
        html.unescape(str(value or ""))
    ).strip().casefold()


def validate_job_company(soup, expected_company):
    og_title = soup.find("meta", property="og:title")

    if not og_title:
        return "unverified"

    page_title = normalized_company_text(og_title.get("content", ""))
    expected_company = normalized_company_text(expected_company)

    if expected_company and expected_company in page_title:
        return "verified"

    return "mismatch"


def validate_description_company(description, expected_company):
    match = re.search(
        r"About The Company\s+(.+)",
        description,
        re.IGNORECASE
    )

    if not match:
        return "unverified"

    description_company = normalized_company_text(match.group(1))
    expected_company = normalized_company_text(expected_company)

    if expected_company and expected_company in description_company:
        return "verified"

    return "mismatch"


def get_job_description(url):
    for attempt in range(3):
        try:
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=30
            )

            requested_job_id = url.rstrip("/").split("/")[-1]

            if requested_job_id not in response.url:
                print(f"Job expired or redirected: {response.url}")
                return "UNAVAILABLE"

            if response.status_code == 404:
                return "UNAVAILABLE"

            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # Extract LinkedIn job metadata
            og_title = soup.find("meta", property="og:title")
            page_title = (
                og_title.get("content", "")
                if og_title else ""
            )

            # Extract job description
            element = soup.find(
                "div",
                class_="show-more-less-html__markup"
            )

            if not element:
                print("Description element not found.")
                return None

            description = element.get_text(
                "\n", strip=True
            )

            if len(description) < MIN_DESCRIPTION_LENGTH:
                print(
                    f"Description too short: {len(description)} characters."
                )
                return None

            # Return description and page metadata
            return {
                "description": description,
                "page_title": page_title
            }

        except requests.RequestException as error:
            print(f"Attempt {attempt + 1} failed: {error}")

            if attempt < 2:
                time.sleep(2 * (attempt + 1))

    return None


def save_description(conn, job_id, description):
    conn.execute("""
        UPDATE jobs
        SET description = ?,
            description_status = 'completed'
        WHERE job_id = ?
    """, (description, job_id))

    conn.commit()

def mark_as_failed(conn, job_id):
    conn.execute("""
        UPDATE jobs
        SET description_status = 'failed'
        WHERE job_id = ?
    """, (job_id,))

    conn.commit()


def mark_as_mismatch(conn, job_id):
    conn.execute("""
        UPDATE jobs
        SET description_status = 'mismatch'
        WHERE job_id = ?
    """, (job_id,))
    conn.commit()


def mark_as_unavailable(conn, job_id):
    conn.execute("""
        UPDATE jobs
        SET description_status = 'unavailable'
        WHERE job_id = ?
    """, (job_id,))
    conn.commit()

def main():
    conn = sqlite3.connect(DB_PATH)

    try:
        jobs = get_jobs(conn)
        batch = jobs[:BATCH_SIZE]

        print(f"Jobs without descriptions: {len(jobs)}")
        print(f"Processing this batch: {len(batch)}")

        saved = 0
        failed = 0

        for index, job in enumerate(batch):

            print(
                f"\n[{index + 1}/{len(batch)}] "
                f"{job['company']} — {job['title']}"
            )

            
            result = get_job_description(job["job_url"])

            if result == "UNAVAILABLE":
                mark_as_unavailable(conn, job["job_id"])
                print("Job is unavailable.")

            elif result:
                description = result["description"]
                page_title = result["page_title"]

                # Check whether the company in the page title
                # matches the company from the email.
                if (
                    normalized_company_text(job["company"])
                    not in normalized_company_text(page_title)
                ):
                    mark_as_failed(conn, job["job_id"])
                    failed += 1

                    print("COMPANY MISMATCH!")
                    print(f"Email company: {job['company']}")
                    print(f"Page title: {page_title}")
                    print("Description not saved.")

                
                else:
                    company_status = validate_description_company(
                        description,
                        job["company"]
                    )

                    if company_status == "verified":
                        save_description(conn, job["job_id"], description)
                        saved += 1
                        print(f"Saved: {len(description)} characters")

                    elif company_status == "mismatch":
                        mark_as_mismatch(conn, job["job_id"])
                        failed += 1
                        print("Company mismatch. Description not trusted.")

                    else:
                        save_description(conn, job["job_id"], description)
                        saved += 1
                        print(
                            f"Saved: {len(description)} characters "
                            "(company verified by LinkedIn page title; "
                            "no company name found in an About section)"
                        )


            else:
                mark_as_failed(conn, job["job_id"])
                failed += 1
                print("Marked as failed.")

            # Be polite to LinkedIn: pause between requests.
            time.sleep(REQUEST_DELAY)


        print("\n--- COLLECTION SUMMARY ---")
        print(f"Successfully saved: {saved}")
        print(f"Skipped or failed: {failed}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
