
import json
import sqlite3
from pathlib import Path

DB_PATH = Path("data/jobs.db")

with sqlite3.connect(DB_PATH) as connection:
    connection.row_factory = sqlite3.Row

    emails = connection.execute("""
        SELECT gmail_id, sender, subject, received_at, body, links
        FROM job_emails
        ORDER BY collected_at DESC
        LIMIT 5
    """).fetchall()

for index, email in enumerate(emails, start=1):
    print("\n" + "=" * 70)
    print(f"EMAIL {index}")
    print("=" * 70)

    print(f"Sender: {email['sender']}")
    print(f"Subject: {email['subject']}")
    print(f"Date: {email['received_at']}")

    print("\nEMAIL BODY:")
    print((email["body"] or "")[:2500])

    links = json.loads(email["links"] or "[]")

    print("\nLINKS:")
    for link in links[:10]:
        print(link)
