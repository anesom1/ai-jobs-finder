import os
import sqlite3
from pathlib import Path


project_root = Path(__file__).resolve().parents[2]
database_path = Path(os.environ["AI_JOB_ASSISTANT_DB"]).resolve()
expected_path = (project_root / "data" / "playwright-test.db").resolve()
if database_path != expected_path:
    raise ValueError(f"Refusing to seed anything except {expected_path}")

database_path.parent.mkdir(parents=True, exist_ok=True)

with sqlite3.connect(database_path) as connection:
    connection.executescript("""
        DROP TABLE IF EXISTS job_tracking;
        DROP TABLE IF EXISTS job_scores;
        DROP TABLE IF EXISTS jobs;

        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT,
            location TEXT,
            job_url TEXT,
            source TEXT,
            source_email_id TEXT,
            collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            description TEXT,
            employment_type TEXT,
            weekly_hours REAL,
            language TEXT,
            seniority TEXT,
            work_mode TEXT,
            description_status TEXT DEFAULT 'pending',
            manual_track TEXT,
            date_posted TEXT,
            archived_at TEXT,
            expired_at TEXT
        );

        CREATE TABLE job_scores (
            job_id TEXT NOT NULL,
            track TEXT NOT NULL,
            score REAL NOT NULL,
            reasons TEXT,
            scored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (job_id, track),
            FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        );

        INSERT INTO jobs (
            job_id, title, company, location, job_url, source,
            description, employment_type, description_status, manual_track
        ) VALUES (
            'e2e-qa-job',
            'QA Automation Engineer',
            'Example Test Company',
            'Remote, Germany',
            'https://example.test/jobs/qa-automation-engineer',
            'Manual',
            'Automated test design and quality engineering.',
            'Full-time',
            'completed',
            'qa'
        );

        INSERT INTO job_scores (job_id, track, score, reasons)
        VALUES ('e2e-qa-job', 'qa', 82, '{}');
    """)
