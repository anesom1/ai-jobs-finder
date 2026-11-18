"""
Run the whole job pipeline once:

    1. gmail_collector.py       - download new job-alert emails
    2. job_extractor.py         - pull the jobs out of those emails
    3. description_collector.py - fetch job descriptions (repeated until none are pending)
    4. scorer.py                - score every job for each career track

Put this file in the project's main folder (next to data/, credentials.json
and .venv).

Run it by hand with:      python run_daily.py
Ignore the "already ran" check:   python run_daily.py --force

Every run is saved in the `sync_runs` table (the dashboard shows the latest one)
and in logs/sync_YYYY-MM-DD.log. The scripts are started from the project
folder, because most of them use the relative path
"data/jobs_before_manual.db".
"""

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "data" / "jobs_before_manual.db"
LOG_DIR = PROJECT_DIR / "logs"

# The four pipeline scripts, relative to the project folder.
GMAIL_SCRIPT = "email_collector/gmail_collector.py"
EXTRACTOR_SCRIPT = "email_collector/job_extractor.py"
DESCRIPTION_SCRIPT = "job_collector/description_collector.py"
SCORER_SCRIPT = "job_scorer/scorer.py"


def project_python():
    """Use the project's .venv Python if it exists, so the scripts always
    find their packages, even when the runner is started by a scheduler."""

    for candidate in (
        PROJECT_DIR / ".venv" / "bin" / "python",
        PROJECT_DIR / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.exists():
            return str(candidate)

    return sys.executable


# Skip the run if a sync finished successfully less than this long ago.
# This lets you schedule "daily" and "at log-in" without running twice.
MIN_HOURS_BETWEEN_RUNS = 20

# description_collector handles 25 jobs per run; repeat it for bigger days.
MAX_DESCRIPTION_ROUNDS = 8


# --------------------------------------------------
# Database helpers
# --------------------------------------------------

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            new_jobs INTEGER DEFAULT 0,
            message TEXT
        )
    """)
    conn.commit()
    return conn


def scalar(conn, sql, default=0):
    """Run a COUNT query; return `default` if the table does not exist yet."""

    try:
        return conn.execute(sql).fetchone()[0]
    except sqlite3.OperationalError:
        return default


def utc_now():
    return datetime.now(timezone.utc)


def last_success_time(conn):
    row = conn.execute("""
        SELECT finished_at FROM sync_runs
        WHERE status = 'success'
        ORDER BY id DESC LIMIT 1
    """).fetchone()

    return datetime.fromisoformat(row[0]) if row and row[0] else None


# --------------------------------------------------
# Running the scripts
# --------------------------------------------------

def log(message, log_file):
    print(message, flush=True)
    log_file.write(message + "\n")
    log_file.flush()


def run_step(title, script, log_file, args=()):
    """Run one pipeline script. Returns (ok, last_output_line)."""

    log(f"\n=== {title} ({datetime.now():%H:%M:%S}) ===", log_file)

    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

    result = subprocess.run(
        [project_python(), str(PROJECT_DIR / script), *args],
        cwd=PROJECT_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    output = (result.stdout or "") + (result.stderr or "")
    log(output.rstrip(), log_file)

    if result.returncode != 0:
        lines = [line for line in output.strip().splitlines() if line.strip()]
        reason = lines[-1] if lines else f"exit code {result.returncode}"
        log(f"!!! {title} failed: {reason}", log_file)
        return False, reason

    return True, ""


def run_descriptions(conn, log_file):
    """Run description_collector until pending and unverified jobs are done."""

    pending_sql = (
        "SELECT COUNT(*) FROM jobs "
        "WHERE description_status IN ('pending', 'unverified')"
    )

    for round_number in range(1, MAX_DESCRIPTION_ROUNDS + 1):
        pending_before = scalar(conn, pending_sql)

        if pending_before == 0:
            if round_number == 1:
                log("\nNo jobs waiting for a description.", log_file)
            return True, ""

        ok, reason = run_step(
            f"Job descriptions, round {round_number} "
            f"({pending_before} pending)",
            DESCRIPTION_SCRIPT,
            log_file
        )

        if not ok:
            return False, reason

        # Safety net: stop if a round made no progress.
        if scalar(conn, pending_sql) >= pending_before:
            return False, "Description collector made no progress"

    left = scalar(conn, pending_sql)
    log(f"\n{left} jobs still pending; they will be done in the next run.",
        log_file)
    return True, ""


# --------------------------------------------------
# Main
# --------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="run even if a sync finished successfully recently"
    )
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    conn = connect()

    last_ok = last_success_time(conn)
    if (
        not args.force
        and last_ok
        and utc_now() - last_ok < timedelta(hours=MIN_HOURS_BETWEEN_RUNS)
    ):
        print(
            f"Last successful sync was at {last_ok.astimezone():%d %b %H:%M}. "
            "Nothing to do (use --force to run anyway)."
        )
        return 0

    started = utc_now()
    run_id = conn.execute(
        "INSERT INTO sync_runs (started_at, status) VALUES (?, 'running')",
        (started.isoformat(),)
    ).lastrowid
    conn.commit()

    log_path = LOG_DIR / f"sync_{datetime.now():%Y-%m-%d}.log"

    status, message, new_jobs = "success", "", 0

    with open(log_path, "a", encoding="utf-8") as log_file:
        log(f"\n##### Sync started {datetime.now():%Y-%m-%d %H:%M:%S} #####",
            log_file)

        try:
            jobs_before = scalar(conn, "SELECT COUNT(*) FROM jobs")

            ok, reason = run_step(
                "1/4 Gmail emails",
                GMAIL_SCRIPT,
                log_file,
                args=["--non-interactive"]
            )

            if ok:
                ok, reason = run_step(
                    "2/4 Extract jobs", EXTRACTOR_SCRIPT, log_file
                )

            if ok:
                new_jobs = (
                    scalar(conn, "SELECT COUNT(*) FROM jobs") - jobs_before
                )
                ok, reason = run_descriptions(conn, log_file)

            if ok:
                ok, reason = run_step("4/4 Scoring", SCORER_SCRIPT, log_file)

            if not ok:
                status, message = "failed", reason

        except Exception as error:  # never leave a run stuck on 'running'
            status, message = "failed", f"{type(error).__name__}: {error}"
            log(f"!!! Unexpected error: {message}", log_file)

        log(
            f"\n##### Sync {status} - {new_jobs} new jobs "
            f"({datetime.now():%H:%M:%S}) #####",
            log_file
        )

    conn.execute("""
        UPDATE sync_runs
        SET finished_at = ?, status = ?, new_jobs = ?, message = ?
        WHERE id = ?
    """, (utc_now().isoformat(), status, new_jobs, message[:500], run_id))
    conn.commit()
    conn.close()

    return 0 if status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
