
import base64
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from email.header import decode_header, make_header
from pathlib import Path

from bs4 import BeautifulSoup
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import time
import random
from googleapiclient.errors import HttpError


# --------------------------------------------------
# 1. Configuration
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent

CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
DATABASE_FILE = BASE_DIR / "data" / "jobs_before_manual.db"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Search for job-alert emails received in the last 14 days.
GMAIL_QUERY = """
newer_than:14d {
    from:jobalerts-noreply@linkedin.com
    from:jobs-noreply@linkedin.com
    from:donotreply@match.indeed.com
}
"""

def execute_with_retry(request, max_attempts=6):
    """Retry Gmail API requests when rate limits are reached."""

    for attempt in range(max_attempts):
        try:
            return request.execute()

        except HttpError as error:
            status = error.resp.status
            content = error.content or b""

            is_rate_limit = (
                status == 429
                or status in (500, 503)
                or (
                    status == 403
                    and (
                        b"rateLimitExceeded" in content
                        or b"userRateLimitExceeded" in content
                    )
                )
            )

            if not is_rate_limit or attempt == max_attempts - 1:
                raise

            wait_time = 2 ** attempt + random.random()

            print(
                f"API limit reached. Retrying in "
                f"{wait_time:.1f} seconds..."
            )

            time.sleep(wait_time)

# --------------------------------------------------
# 2. Connect to Gmail
# --------------------------------------------------

def authenticate_gmail(interactive=True):
    """Authenticate with Google and return the Gmail service.

    interactive=False is used by the daily sync: if a new login is needed it
    stops with a clear message instead of opening a browser nobody can click.
    """

    credentials = None

    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(
            str(TOKEN_FILE), SCOPES
        )

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            TOKEN_FILE.write_text(credentials.to_json())

        except RefreshError:
            # The saved login was revoked or has expired.
            credentials = None

    if not credentials or not credentials.valid:
        if not interactive:
            raise SystemExit(
                "Gmail login is missing or expired. Run "
                "'python gmail_collector.py' once by hand to log in again."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE), SCOPES
        )

        credentials = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(credentials.to_json())

    return build("gmail", "v1", credentials=credentials)


# --------------------------------------------------
# 3. Decode email headers and content
# --------------------------------------------------

def decode_header_value(value):
    """Decode an email header into readable text."""

    if not value:
        return ""

    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def decode_body_data(data):
    """Decode Gmail's URL-safe Base64 body data."""

    if not data:
        return ""

    padding = "=" * (-len(data) % 4)
    decoded = base64.urlsafe_b64decode(data + padding)

    return decoded.decode("utf-8", errors="replace")


def extract_email_parts(payload):
    """Extract plain text and HTML content from an email."""

    plain_parts = []
    html_parts = []

    def walk(part):
        mime_type = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")

        if body_data:
            content = decode_body_data(body_data)

            if mime_type == "text/plain":
                plain_parts.append(content)

            elif mime_type == "text/html":
                html_parts.append(content)

        for child in part.get("parts", []):
            walk(child)

    walk(payload)

    if plain_parts:
        return "\n".join(plain_parts)

    if html_parts:
        soup = BeautifulSoup("\n".join(html_parts), "html.parser")
        return soup.get_text(separator="\n", strip=True)

    return ""


# --------------------------------------------------
# 4. Extract links from emails
# --------------------------------------------------

def extract_links(payload, body):
    """Extract links from HTML and plain-text email content."""

    links = []

    def walk(part):
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data")

            if data:
                html = decode_body_data(data)
                soup = BeautifulSoup(html, "html.parser")

                for anchor in soup.find_all("a", href=True):
                    links.append(anchor["href"])

        for child in part.get("parts", []):
            walk(child)

    walk(payload)

    # Also look for URLs directly in the email text.
    links.extend(re.findall(r"https?://[^\s<>\"']+", body))

    # Remove duplicates and common trailing punctuation.
    cleaned_links = []

    for link in links:
        link = link.rstrip(".,;:!?) ]")

        if link.startswith("http") and link not in cleaned_links:
            cleaned_links.append(link)

    return cleaned_links


# --------------------------------------------------
# 5. Create local database
# --------------------------------------------------

def initialize_database():
    """Create the SQLite database and email table."""

    DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DATABASE_FILE) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS job_emails (
                gmail_id TEXT PRIMARY KEY,
                sender TEXT,
                subject TEXT,
                received_at TEXT,
                body TEXT,
                links TEXT,
                collected_at TEXT
            )
        """)

        connection.commit()


# --------------------------------------------------
# 6. Collect job-alert emails
# --------------------------------------------------

def collect_job_emails(interactive=True):
    """Find job-alert emails and save them to SQLite."""

    print("Connecting to Gmail...")

    service = authenticate_gmail(interactive=interactive)

    initialize_database()

    print("Searching for job-alert emails...")

    next_page_token = None
    new_emails = 0
    total_found = 0

    with sqlite3.connect(DATABASE_FILE) as connection:

        while True:
            results = execute_with_retry(
                service.users().messages().list(
                    userId="me",
                    q=GMAIL_QUERY,
                    maxResults=20,
                    pageToken=next_page_token
                )
            )

            messages = results.get("messages", [])
            total_found += len(messages)

            for message in messages:
                # Skip emails already stored in our database.
                existing = connection.execute(
                    "SELECT 1 FROM job_emails WHERE gmail_id = ?",
                    (message["id"],)
                ).fetchone()

                if existing:
                    continue

                email_data = execute_with_retry(
                    service.users().messages().get(
                        userId="me",
                        id=message["id"],
                        format="full"
                    )
                )

                payload = email_data.get("payload", {})
                headers = payload.get("headers", [])

                header_dict = {
                    header["name"].lower(): header["value"]
                    for header in headers
                }

                sender = decode_header_value(
                    header_dict.get("from", "")
                )

                subject = decode_header_value(
                    header_dict.get("subject", "")
                )

                received_at = header_dict.get("date", "")

                body = extract_email_parts(payload)

                links = extract_links(payload, body)

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO job_emails (
                        gmail_id,
                        sender,
                        subject,
                        received_at,
                        body,
                        links,
                        collected_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message["id"],
                        sender,
                        subject,
                        received_at,
                        body,
                        json.dumps(links),
                        datetime.now(timezone.utc).isoformat()
                    )
                )

                if cursor.rowcount == 1:
                    new_emails += 1

                connection.commit()

            connection.commit()

            next_page_token = results.get("nextPageToken")

            if not next_page_token:
                break

    print("\nGmail collection completed!")
    print(f"Emails found: {total_found}")
    print(f"New emails saved: {new_emails}")
    print(f"Database: {DATABASE_FILE}")


# --------------------------------------------------
# 7. Run the collector
# --------------------------------------------------

if __name__ == "__main__":
    collect_job_emails(interactive="--non-interactive" not in sys.argv)
