import sqlite3
import json

conn = sqlite3.connect("data/jobs.db")

for track in ["qa", "ai", "ai_testing"]:
    print("\n" + "=" * 70)
    print("TOP 5 — " + track.upper())
    print("=" * 70)

    rows = conn.execute("""
        SELECT
            j.title,
            j.company,
            j.location,
            s.score,
            s.reasons,
            SUBSTR(j.description, 1, 500)
        FROM job_scores s
        JOIN jobs j ON j.job_id = s.job_id
        WHERE s.track = ?
        ORDER BY s.score DESC
        LIMIT 5
    """, (track,)).fetchall()

    for title, company, location, score, reasons, description in rows:
        print(f"\n{title} | {company}")
        print(f"Location: {location} | Score: {score}")

        print("Scoring reasons:")
        print(json.dumps(json.loads(reasons), indent=2, ensure_ascii=False))

        print("Description preview:")
        print(description or "No description")

conn.close()