import importlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4
from datetime import date, datetime

import pandas as pd
import streamlit as st


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "jobs_before_manual.db"

# Lets the dashboard import the scorer (job_scorer/scorer.py) for manual jobs.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from job_collector import linkedin_importer

if not callable(getattr(linkedin_importer, "fetch_job_offer", None)):
    linkedin_importer = importlib.reload(linkedin_importer)

from job_collector.expiration_checker import check_linkedin_jobs

st.set_page_config(
    page_title="AI Job Assistant",
    page_icon="💼",
    layout="wide"
)


# --------------------------------------------------
# DATABASE CONNECTION AND TRACKING TABLE
# --------------------------------------------------

def get_connection():
    return sqlite3.connect(DB_PATH)


def create_tracking_table():
    """Create the tracking table and add missing columns if necessary."""

    conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_tracking (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            application_date TEXT,
            stage TEXT DEFAULT 'Not started',
            notes TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    existing_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(job_tracking)")
    }

    new_columns = {
        "application_date": "TEXT",
        "stage": "TEXT DEFAULT 'Not started'",
        "notes": "TEXT"
    }

    for column, data_type in new_columns.items():
        if column not in existing_columns:
            conn.execute(
                f"ALTER TABLE job_tracking ADD COLUMN {column} {data_type}"
            )

    conn.commit()
    conn.close()


# --------------------------------------------------
# JOB STATUS AND APPLICATION UPDATES
# --------------------------------------------------

def ensure_manual_job_column():
    """Add columns needed to track manually added jobs."""

    conn = get_connection()

    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}

    new_columns = {
        "manual_track": "TEXT",
        "date_posted": "TEXT",
        "archived_at": "TEXT",
        "expired_at": "TEXT",
    }

    for column, data_type in new_columns.items():
        if columns and column not in columns:
            conn.execute(
                f"ALTER TABLE jobs ADD COLUMN {column} {data_type}"
            )
            columns.add(column)

    if columns:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deleted_jobs (
                job_id TEXT PRIMARY KEY,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

    conn.close()


def update_job_status(job_id, status):
    """Save a status and preserve any existing application details."""

    conn = get_connection()

    # When a job is first marked as Applied, record today's date.
    # Do not overwrite an application date that is already stored.
    conn.execute("""
        INSERT INTO job_tracking (
            job_id,
            status,
            application_date,
            stage
        )
        VALUES (?, ?, ?, ?)

        ON CONFLICT(job_id)
        DO UPDATE SET
            status = excluded.status,
            application_date = CASE
                WHEN excluded.status = 'Applied'
                     AND (
                         job_tracking.application_date IS NULL
                         OR job_tracking.application_date = ''
                     )
                THEN excluded.application_date
                ELSE job_tracking.application_date
            END,
            stage = CASE
                WHEN excluded.status = 'Applied'
                     AND (
                         job_tracking.stage IS NULL
                         OR job_tracking.stage = ''
                         OR job_tracking.stage = 'Not started'
                     )
                THEN 'Applied'
                ELSE job_tracking.stage
            END,
            updated_at = CURRENT_TIMESTAMP
    """, (
        str(job_id),
        status,
        date.today().isoformat() if status == "Applied" else None,
        "Applied" if status == "Applied" else "Not started"
    ))

    conn.commit()
    conn.close()


def reset_job_status(job_id):
    """Clear a job's tracking data so it counts as 'New' again.

    This removes the tracking row, including stage, application date and notes.
    """

    conn = get_connection()
    conn.execute("DELETE FROM job_tracking WHERE job_id = ?", (str(job_id),))
    conn.commit()
    conn.close()


@st.dialog("Mark as new?")
def confirm_reset_dialog(job_id, title):
    """Ask for confirmation before clearing a job's tracking data."""

    st.write(f"Clear the status of **{title}** and mark it as new again?")
    st.caption("Its stage, application date and notes will be removed.")

    yes_col, cancel_col = st.columns(2)

    with yes_col:
        if st.button("Yes, reset", type="primary", **STRETCH):
            reset_job_status(job_id)
            st.rerun()

    with cancel_col:
        if st.button("Cancel", **STRETCH):
            st.rerun()


def update_application_details(
    job_id,
    status,
    application_date,
    stage,
    notes
):
    """Save application details without losing existing tracking data."""

    conn = get_connection()

    conn.execute("""
        INSERT INTO job_tracking (
            job_id,
            status,
            application_date,
            stage,
            notes
        )
        VALUES (?, ?, ?, ?, ?)

        ON CONFLICT(job_id)
        DO UPDATE SET
            status = excluded.status,
            application_date = excluded.application_date,
            stage = excluded.stage,
            notes = excluded.notes,
            updated_at = CURRENT_TIMESTAMP
    """, (
        str(job_id),
        status,
        application_date,
        stage,
        notes
    ))

    conn.commit()
    conn.close()


def update_job_notes(job_id, notes):
    """Save notes without changing the job's existing tracking details."""

    conn = get_connection()
    conn.execute("""
        INSERT INTO job_tracking (job_id, status, notes)
        VALUES (?, 'New', ?)
        ON CONFLICT(job_id)
        DO UPDATE SET
            notes = excluded.notes,
            updated_at = CURRENT_TIMESTAMP
    """, (str(job_id), notes.strip() or None))
    conn.commit()
    conn.close()


# --------------------------------------------------
# SHARED CONSTANTS AND SMALL HELPERS
# --------------------------------------------------

STATUS_ICONS = {
    "New": "🆕",
    "Saved": "⭐",
    "Applied": "📩",
    "Rejected": "❌"
}

ALL_STAGES = [
    "Not started",
    "Applied",
    "HR screening",
    "Interview",
    "Technical interview",
    "Final interview",
    "Offer",
    "Hired",
    "No response",
    "Rejected"
]

MANUAL_SOURCE = "Manual"

TRACK_OPTIONS = {
    "QA / Software Testing": "qa",
    "AI / Machine Learning": "ai",
    "AI Testing": "ai_testing"
}

EMPLOYMENT_TYPES = [
    "Not specified", "Full-time", "Part-time", "Contract", "Internship"
]

# `use_container_width` is deprecated in newer Streamlit versions and
# replaced by `width="stretch"`. Pick whichever the installed version knows.
_version = tuple(int(p) for p in st.__version__.split(".")[:2])
STRETCH = (
    {"width": "stretch"}
    if _version >= (1, 50)
    else {"use_container_width": True}
)

st.markdown("""
<style>
[data-testid="stAppViewContainer"] {
    background:
        linear-gradient(180deg, rgba(99, 102, 241, 0.035), transparent 24rem),
        var(--background-color);
}
[data-testid="stHeader"] {
    background: transparent;
}
[data-testid="stMainBlockContainer"] {
    max-width: 1440px;
    padding-top: 2rem;
    padding-bottom: 3rem;
}
[data-testid="stAppViewContainer"] h1 {
    font-size: clamp(2.2rem, 3.2vw, 2.8rem);
    letter-spacing: -0.045em;
    font-weight: 720;
    margin-bottom: 0.25rem;
}
[data-testid="stAppViewContainer"] h2,
[data-testid="stAppViewContainer"] h3 {
    letter-spacing: -0.025em;
}
[data-testid="stAppViewContainer"] [data-testid="stCaptionContainer"] {
    opacity: 0.78;
}
[data-testid="stTabs"] [role="tablist"] {
    gap: 0.35rem;
    border-bottom-color: rgba(148, 163, 184, 0.22);
}
[data-testid="stTabs"] [role="tab"] {
    font-weight: 600;
    border-radius: 10px 10px 0 0;
    padding: 0.7rem 1.05rem;
    font-size: 1.08rem;
    transition: color 140ms ease, background-color 140ms ease;
}
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
    border-bottom-color: #c53948 !important;
    color: #c53948 !important;
    background: rgba(197, 57, 72, 0.055);
}
[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 16px;
}
[class*="st-key-jobs_job_card_"],
[class*="st-key-best_"][class*="_job_card_"],
[class*="st-key-archived_jobs_job_card_"],
[class*="st-key-tracker_job_card_"] {
    background: var(--secondary-background-color);
    border-top: 1px solid rgba(148, 163, 184, 0.20);
    border-right: 1px solid rgba(148, 163, 184, 0.20);
    border-bottom: 1px solid rgba(148, 163, 184, 0.20);
    border-radius: 16px;
    box-shadow: 0 4px 16px rgba(15, 23, 42, 0.055);
    padding: 0.6rem;
    transition: box-shadow 150ms ease, transform 150ms ease;
}
[class*="st-key-jobs_job_card_"]:hover,
[class*="st-key-best_"][class*="_job_card_"]:hover,
[class*="st-key-archived_jobs_job_card_"]:hover,
[class*="st-key-tracker_job_card_"]:hover {
    box-shadow: 0 9px 24px rgba(15, 23, 42, 0.10);
    transform: translateY(-2px);
}
.stButton > button,
[data-testid="stLinkButton"] a {
    border-radius: 10px;
    font-weight: 550;
    min-height: 2.5rem;
    min-width: 2.5rem;
    padding: 0.45rem 0.65rem;
    font-size: 1rem;
    transition: border-color 140ms ease, box-shadow 140ms ease,
        transform 140ms ease;
}
.stButton > button:hover,
[data-testid="stLinkButton"] a:hover {
    transform: translateY(-1px);
}
[class*="st-key-jobs_job_card_"] button[data-testid^="stBaseButton"],
[class*="st-key-best_"][class*="_job_card_"] button[data-testid^="stBaseButton"],
[class*="st-key-archived_jobs_job_card_"] button[data-testid^="stBaseButton"],
[class*="st-key-tracker_job_card_"] button[data-testid^="stBaseButton"],
[class*="st-key-jobs_job_card_"] [data-testid="stLinkButton"] a,
[class*="st-key-best_"][class*="_job_card_"] [data-testid="stLinkButton"] a,
[class*="st-key-archived_jobs_job_card_"] [data-testid="stLinkButton"] a,
[class*="st-key-tracker_job_card_"] [data-testid="stLinkButton"] a {
    min-height: 1.75rem;
    min-width: 1.75rem;
    padding: 0.15rem 0.3rem;
    font-size: 0.82rem;
}
[class*="st-key-jobs_job_card_"] [class*="st-key-jobs_save_"],
[class*="st-key-jobs_job_card_"] [class*="st-key-jobs_reject_"],
[class*="st-key-jobs_job_card_"] [class*="st-key-jobs_applied_"],
[class*="st-key-jobs_job_card_"] [class*="st-key-jobs_reset_"],
[class*="st-key-jobs_job_card_"] [class*="st-key-jobs_archive_"],
[class*="st-key-jobs_job_card_"] [class*="st-key-jobs_delete_"] {
    margin-inline: -0.2rem;
}
[class*="st-key-jobs_row_actions_"] [data-testid="stLinkButton"] a,
[class*="st-key-tracker_row_actions_"] [data-testid="stLinkButton"] a,
[class*="st-key-archived_jobs_row_actions_"] [data-testid="stLinkButton"] a,
[class*="st-key-best_"][class*="_row_actions_"] [data-testid="stLinkButton"] a {
    display: inline-flex;
    align-items: center;
    min-height: 1.3rem;
    min-width: 1.3rem;
    padding: 0.02rem 0.05rem;
    font-size: 0.75rem;
    border-radius: 7px;
}
[class*="st-key-jobs_job_card_"] [class*="st-key-jobs_details_"] button,
[class*="st-key-best_"][class*="_job_card_"] [class*="_details_"] button,
[class*="st-key-archived_jobs_job_card_"] [class*="_details_"] button,
[class*="st-key-tracker_job_card_"] [class*="_details_"] button {
    justify-content: flex-start;
    text-align: left;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-size: 1.75rem;
    font-weight: 700;
    min-height: 2.5rem;
    padding: 0.1rem 0;
}
[class*="st-key-jobs_job_card_"] [class*="_details_"] button > div,
[class*="st-key-best_"][class*="_job_card_"] [class*="_details_"] button > div,
[class*="st-key-archived_jobs_job_card_"] [class*="_details_"] button > div,
[class*="st-key-tracker_job_card_"] [class*="_details_"] button > div,
[class*="_row_title_"] [class*="_details_"] button > div {
    width: 100%;
    justify-content: flex-start;
}
[class*="st-key-jobs_job_card_"] [class*="_details_"] button > div > span,
[class*="st-key-best_"][class*="_job_card_"] [class*="_details_"] button > div > span,
[class*="st-key-archived_jobs_job_card_"] [class*="_details_"] button > div > span,
[class*="st-key-tracker_job_card_"] [class*="_details_"] button > div > span,
[class*="_row_title_"] [class*="_details_"] button > div > span {
    width: 100%;
    flex: 1;
    justify-content: flex-start;
}
[class*="_job_card_"] [data-testid="stVerticalBlock"] {
    gap: 0.45rem;
}
[class*="_job_card_"] [data-testid="stMarkdownContainer"] p {
    margin-bottom: 0.25rem;
}
[class*="st-key-jobs_job_card_"] [class*="_details_"] button p,
[class*="st-key-best_"][class*="_job_card_"] [class*="_details_"] button p,
[class*="st-key-archived_jobs_job_card_"] [class*="_details_"] button p,
[class*="st-key-tracker_job_card_"] [class*="_details_"] button p {
    width: 100%;
    font-size: 1.75rem !important;
    font-weight: inherit;
    line-height: 1.3;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-align: left;
}
[class*="_row_title_"] [class*="_details_"] button p {
    width: 100%;
    text-align: left;
}
[data-testid="stDialog"] [data-testid="stMarkdownContainer"] h3 {
    font-size: 1rem;
    letter-spacing: -0.01em;
    margin: 0.8rem 0 0.25rem;
}
[data-testid="stButton"] button[kind="tertiary"] {
    justify-content: flex-start;
    text-align: left;
    font-weight: 650;
    padding-left: 0;
}
.st-key-add_job_button button[data-testid^="stBaseButton"] {
    background: #2563eb;
    border-color: #2563eb;
    color: #fff;
}
.st-key-add_job_button button[data-testid^="stBaseButton"]:hover {
    background: #1d4ed8;
    border-color: #1d4ed8;
    color: #fff;
}
.st-key-jobs_collection_toolbar button[data-testid^="stBaseButton"] {
    min-height: 1.75rem;
    min-width: 1.75rem;
    padding: 0.15rem 0.3rem;
    font-size: 0.8rem;
}
.st-key-jobs_collection_toolbar [data-baseweb="select"] > div,
.st-key-jobs_collection_toolbar [data-baseweb="input"] > div {
    min-height: 2rem;
}
[class*="st-key-jobs_row_actions_"] button[data-testid^="stBaseButton"],
[class*="st-key-tracker_row_actions_"] button[data-testid^="stBaseButton"],
[class*="st-key-archived_jobs_row_actions_"] button[data-testid^="stBaseButton"],
[class*="st-key-best_"][class*="_row_actions_"] button[data-testid^="stBaseButton"] {
    min-height: 1.3rem;
    min-width: 1.3rem;
    padding: 0.02rem 0.05rem;
    font-size: 0.75rem;
    border-radius: 7px;
}
[class*="st-key-jobs_row_title_"] [data-testid="stVerticalBlock"],
[class*="st-key-tracker_row_title_"] [data-testid="stVerticalBlock"] {
    gap: 0;
}
[class*="_row_title_"] [class*="_details_"] button {
    min-height: 1.65rem;
    padding: 0;
}
[class*="st-key-jobs_list_item_"] [class*="_row_title_"] [class*="_details_"] button > div > span,
[class*="st-key-best_"][class*="_list_item_"] [class*="_row_title_"] [class*="_details_"] button > div > span,
[class*="st-key-archived_jobs_list_item_"] [class*="_row_title_"] [class*="_details_"] button > div > span {
    width: 24%;
    max-width: 24%;
    min-width: 0;
    flex: 0 1 24%;
}
[class*="st-key-tracker_list_item_"] [class*="_row_title_"] [class*="_details_"] button > div > span,
[class*="st-key-tracker_month_"] [class*="_row_title_"] [class*="_details_"] button > div > span {
    width: 28%;
    max-width: 28%;
    min-width: 0;
    flex: 0 1 28%;
}
[class*="_row_title_"] [class*="_details_"] button > div > span > span > [data-testid="stMarkdownContainer"],
[class*="_row_title_"] [class*="_details_"] button p {
    display: block;
    width: 100%;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
[class*="_row_title_"] [class*="_details_"] button > div > span {
    width: 100%;
    min-width: 0;
}
[class*="_row_title_"] [data-testid="stCaptionContainer"] {
    margin-top: 0.1rem;
}
[class*="_row_title_"] [data-testid="stCaptionContainer"] p {
    margin-bottom: 0;
    line-height: 1.2;
}
[class*="_list_item_"] {
    position: relative;
    min-height: 4.9rem;
    border-bottom: 1px solid rgba(148, 163, 184, 0.22);
    padding-top: 0.1rem;
    padding-bottom: 0.25rem;
    transition: background-color 140ms ease;
}
[class*="_list_item_"] [data-testid="stHorizontalBlock"] {
    min-height: 4.4rem;
}
[class*="_list_item_"]:hover {
    background-color: rgba(148, 163, 184, 0.08) !important;
}
[class*="_list_item_"]:hover [data-testid="stHorizontalBlock"] {
    background-color: rgba(148, 163, 184, 0.08) !important;
}
[class*="_list_item_"] [class*="_row_title_"] [class*="_details_"] {
    position: absolute;
    inset: 0;
    z-index: 1;
}
[class*="_list_item_"] [class*="_row_title_"] [class*="_details_"] button {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    align-items: flex-start;
    cursor: pointer;
}
[class*="_list_item_"] [class*="_row_title_"] [class*="_details_"] button > div,
[class*="_list_item_"] [class*="_row_title_"] [class*="_details_"] button > div > span {
    align-items: flex-start;
}
[class*="_list_item_"] [class*="_row_actions_"] {
    position: relative;
    z-index: 2;
}
[class*="_list_view"] [data-testid="stMarkdownContainer"] p,
[class*="_list_view"] [data-testid="stCaptionContainer"] p,
[class*="_list_view"] button p {
    font-size: 14px !important;
}
[class*="_list_view"] [data-testid="stVerticalBlock"] {
    gap: 0.25rem;
}
[class*="_list_view"] [data-testid="stElementContainer"] {
    margin-bottom: 0;
}
[data-baseweb="input"] > div,
[data-baseweb="select"] > div,
[data-baseweb="textarea"] {
    border-radius: 10px;
}
[data-baseweb="input"] > div:focus-within,
[data-baseweb="select"] > div:focus-within,
[data-baseweb="textarea"]:focus-within {
    border-color: #c53948;
    box-shadow: 0 0 0 1px #c53948;
}
[class*="st-key-jobs_job_card_new_"],
[class*="st-key-best_"][class*="_job_card_new_"],
[class*="st-key-archived_jobs_job_card_new_"],
[class*="st-key-tracker_job_card_new_"] {
    background-color: rgba(59, 130, 246, 0.09);
    border-left: 4px solid #3b82f6;
}
[class*="st-key-jobs_job_card_saved_"],
[class*="st-key-best_"][class*="_job_card_saved_"],
[class*="st-key-archived_jobs_job_card_saved_"],
[class*="st-key-tracker_job_card_saved_"] {
    background-color: rgba(234, 179, 8, 0.10);
    border-left: 4px solid #eab308;
}
[class*="st-key-jobs_job_card_applied_"],
[class*="st-key-best_"][class*="_job_card_applied_"],
[class*="st-key-archived_jobs_job_card_applied_"],
[class*="st-key-tracker_job_card_applied_"] {
    background-color: rgba(34, 197, 94, 0.09);
    border-left: 4px solid #22c55e;
}
[class*="st-key-jobs_job_card_rejected_"],
[class*="st-key-best_"][class*="_job_card_rejected_"],
[class*="st-key-archived_jobs_job_card_rejected_"],
[class*="st-key-tracker_job_card_rejected_"] {
    background-color: rgba(239, 68, 68, 0.09);
    border-left: 4px solid #ef4444;
}
@media (max-width: 768px) {
    [data-testid="stMainBlockContainer"] {
        padding: 1.25rem 1rem 2rem;
    }
}
</style>
""", unsafe_allow_html=True)


def clean(value, fallback=""):
    """Return a display string, or a fallback for None / NaN / empty."""

    if value is None or pd.isna(value) or str(value).strip() == "":
        return fallback

    return str(value)


def clean_notes(value):
    """Hide legacy textual NaN values in optional job notes."""

    text = clean(value)
    return "" if text.casefold() == "nan" else text


def set_session_state_value(key, value):
    """Set dialog state from a Streamlit widget callback."""

    st.session_state[key] = value


DESCRIPTION_HEADING_PATTERN = re.compile(
    r"^(?:"
    r"about(?: the)? (?:role|job|company)|"
    r"(?:your|the) (?:responsibilit\w*|profile|qualifications?|skills|"
    r"tasks?|experience|requirements?)|"
    r"requirements?|qualifications?|required skills?|skills?|"
    r"responsibilit\w*|tasks?|experience|benefits?(?:\s+at\s+.*)?|"
    r"(?:our|what we|what you|what you will|what you bring)(?:\s+.*)?|"
    r"nice to have|strong plus|must have|ideal(?:ly)?|"
    r"dein(?:e)?\s+.*|das bringst du mit|überblick|"
    r"the qa craft|the ai side"
    r")$",
    re.IGNORECASE
)
DESCRIPTION_LIST_HEADING_PATTERN = re.compile(
    r"(?:responsibilit|profile|qualification|requirement|skill|"
    r"task|experience|benefit|offer|bringst|nice to have|strong plus|"
    r"must have|what we value|qa craft|ai side)",
    re.IGNORECASE
)


def _is_description_heading(line):
    """Recognize common standalone headings in scraped job descriptions."""

    text = line.strip().rstrip(":")
    return bool(DESCRIPTION_HEADING_PATTERN.fullmatch(text))


def _description_list_item(line):
    """Remove existing bullet markers before adding consistent Markdown."""

    return re.sub(r"^[•*\-]\s*", "", line)


def format_job_description(description):
    """Format scraped descriptions into readable headings and bullet lists."""

    lines = [
        line.strip()
        for line in clean(description).replace("\r\n", "\n").split("\n")
        if line.strip()
    ]
    if not lines:
        return "No description available."

    blocks = []
    current_heading = None
    body_lines = []

    def flush_block():
        if current_heading:
            blocks.append(f"### {current_heading}")
        if not body_lines:
            return

        is_list = bool(
            current_heading
            and DESCRIPTION_LIST_HEADING_PATTERN.search(current_heading)
        )
        if is_list:
            blocks.append("\n".join(
                f"- {_description_list_item(line)}"
                for line in body_lines
            ))
        else:
            blocks.append(" ".join(body_lines))

    for line in lines:
        if _is_description_heading(line):
            flush_block()
            current_heading = line.rstrip(":")
            body_lines = []
        else:
            body_lines.append(line)

    flush_block()
    return "\n\n".join(blocks)


def description_preview(description, max_characters=700):
    """Return a short opening excerpt without cutting in the middle of a word."""

    text = clean(description)
    if len(text) <= max_characters:
        return text

    excerpt = text[:max_characters].rsplit(None, 1)[0].rstrip(" ,;:-")
    return f"{excerpt}…"


def company_line(job):
    """Company name, with a marker for jobs I added by hand."""

    company = clean(job["company"])

    if job.get("source") == MANUAL_SOURCE:
        return f"{company} · ✍️ Manual"

    return company


def score_text(score):
    """Match score for display. 0 means the track does not fit the role."""

    return f"{score:.0f}%" if score > 0 else "–"


def apply_filters(df, statuses, stages):
    """Filter by status and stage. An empty selection means 'no filter'."""

    if statuses:
        df = df[df["status"].isin(statuses)]

    if stages:
        df = df[df["stage"].isin(stages)]

    return df


def apply_job_search(df, query):
    """Keep jobs whose title or company contains the search text."""

    query = (query or "").strip()
    if not query:
        return df

    matches = (
        df["title"].fillna("").astype(str).str.contains(query, case=False, regex=False)
        | df["company"].fillna("").astype(str).str.contains(
            query, case=False, regex=False
        )
    )
    return df[matches]


ADDED_OPTIONS = {
    "Any time": None,
    "Today": 0,
    "Last 7 days": 6,
    "Last 30 days": 29
}


def apply_added_filter(df, choice):
    """Keep only jobs added within the chosen period."""

    days_back = ADDED_OPTIONS[choice]

    if days_back is None:
        return df

    tz = datetime.now().astimezone().tzinfo
    cutoff = pd.Timestamp.now(tz=tz).normalize() - pd.Timedelta(days=days_back)

    return df[df["added_at"] >= cutoff]


def format_added(value):
    """Date a job was added to the database, e.g. '13 Nov 2026'."""

    if pd.isna(value):
        return "–"

    return value.strftime("%d %b %Y")


def update_bulk_selection(selection_key, widget_key, job_id):
    """Keep the selected job IDs synchronized with the visible checkboxes."""

    selected_ids = st.session_state.setdefault(selection_key, [])
    if st.session_state.get(widget_key, False):
        if job_id not in selected_ids:
            selected_ids.append(job_id)
    elif job_id in selected_ids:
        selected_ids.remove(job_id)


def toggle_bulk_actions(mode_key, selection_key, generation_key):
    """Toggle bulk-selection mode and invalidate old checkbox widget state."""

    st.session_state[mode_key] = not st.session_state.get(mode_key, False)
    st.session_state[selection_key] = []
    st.session_state[generation_key] = (
        st.session_state.get(generation_key, 0) + 1
    )


def render_bulk_checkbox(job, selection_key, generation):
    """Render one job's bulk-selection checkbox."""

    job_id = str(job["job_id"])
    widget_key = f"{selection_key}_{generation}_{job_id}"
    st.checkbox(
        "Select job",
        key=widget_key,
        value=job_id in st.session_state.get(selection_key, []),
        label_visibility="collapsed",
        help=f"Select {clean(job['title'], 'job')}",
        on_change=update_bulk_selection,
        args=(selection_key, widget_key, job_id)
    )


def show_collection(
    df,
    key,
    render_card,
    render_row,
    list_widths,
    list_headers,
    bulk_actions=False
):
    """
    Show a DataFrame of jobs either as a compact card grid or as a list,
    with a view switch and simple pagination.
    """

    n = len(df)

    with st.container(key=f"{key}_collection_toolbar"):
        view_area, controls_area = st.columns([2.2, 1.8], gap="small")

        with view_area:
            view = st.segmented_control(
                "View",
                ["Cards", "List"],
                default="Cards",
                format_func=lambda v: {
                    "Cards": "🗂️ Cards",
                    "List": "📃 List"
                }[v],
                key=f"{key}_view",
                label_visibility="collapsed"
            ) or "Cards"

        with controls_area:
            (
                per_row_col,
                page_size_col,
                previous_col,
                page_col,
                next_col
            ) = st.columns(
                [1.15, 1.15, 1.05, 0.75, 1.05],
                gap="small",
                vertical_alignment="center"
            )

            per_row = 3
            if view == "Cards":
                per_row = per_row_col.selectbox(
                    "Cards per row",
                    [2, 3, 4, 5],
                    index=1,
                    key=f"{key}_per_row",
                    label_visibility="collapsed",
                    help="Cards per row"
                )

            page_size = page_size_col.selectbox(
                "Per page",
                [12, 24, 48, 100],
                index=1,
                key=f"{key}_page_size",
                label_visibility="collapsed",
                help="Jobs per page"
            )

    pages = max(1, -(-n // page_size))

    page_key = f"{key}_page_{n}_{page_size}"
    page = 1
    if pages > 1:
        # The key includes the result count so the page resets to 1
        # whenever the filters change the number of results.
        page = min(max(int(st.session_state.get(page_key, 1)), 1), pages)
        with previous_col:
            if st.button(
                "‹ Prev",
                key=f"{key}_previous_{n}_{page_size}",
                help="Previous page",
                disabled=page <= 1,
                **STRETCH
            ):
                st.session_state[page_key] = page - 1
                st.rerun()

        with page_col:
            page = st.number_input(
                f"Page (of {pages})",
                min_value=1,
                max_value=pages,
                value=page,
                key=page_key,
                label_visibility="collapsed",
                help=f"Page number, {pages} pages"
            )

        with next_col:
            if st.button(
                "Next ›",
                key=f"{key}_next_{n}_{page_size}",
                help="Next page",
                disabled=page >= pages,
                **STRETCH
            ):
                st.session_state[page_key] = page + 1
                st.rerun()

    start = (page - 1) * page_size
    page_df = df.iloc[start:start + page_size]

    if bulk_actions and n:
        selection_key = f"{key}_bulk_selection"
        mode_key = f"{key}_bulk_mode"
        generation_key = f"{selection_key}_generation"
        generation = st.session_state.setdefault(generation_key, 0)
        filtered_ids = set(df["job_id"].astype(str))
        selected_ids = st.session_state.get(selection_key, [])
        filtered_selection = [
            job_id
            for job_id in selected_ids
            if job_id in filtered_ids
        ]
        if filtered_selection != selected_ids:
            st.session_state[selection_key] = filtered_selection
            st.session_state[generation_key] += 1
            generation = st.session_state[generation_key]
        bulk_mode = st.session_state.get(mode_key, False)

        toolbar_cols = st.columns([1.6, 1.1, 1.1, 4.5])
        with toolbar_cols[0]:
            st.button(
                "Bulk actions" if not bulk_mode else "Done",
                key=f"{key}_bulk_toggle",
                on_click=toggle_bulk_actions,
                args=(mode_key, selection_key, generation_key),
                **STRETCH
            )

        if bulk_mode:
            selected_ids = st.session_state[selection_key]
            with toolbar_cols[1]:
                if st.button(
                    "Archive",
                    key=f"{key}_bulk_archive",
                    disabled=not selected_ids,
                    **STRETCH
                ):
                    try:
                        archive_jobs(selected_ids)
                    except ValueError as error:
                        st.error(str(error))
                        return
                    st.session_state[selection_key] = []
                    st.session_state[generation_key] += 1
                    st.toast(f"Archived {len(selected_ids)} jobs.")
                    st.rerun()

            with toolbar_cols[2]:
                if st.button(
                    "Delete",
                    key=f"{key}_bulk_delete",
                    disabled=not selected_ids,
                    **STRETCH
                ):
                    confirm_bulk_delete_dialog(
                        list(selected_ids),
                        selection_key,
                        generation_key
                    )

            toolbar_cols[3].caption(
                f"{len(selected_ids)} selected" if selected_ids else ""
            )

    else:
        selection_key = f"{key}_bulk_selection"
        mode_key = f"{key}_bulk_mode"
        generation_key = f"{selection_key}_generation"
        generation = st.session_state.get(generation_key, 0)
        bulk_mode = False

    if bulk_actions and bulk_mode:
        list_row_widths = [0.35, *list_widths]
        list_header_labels = ["", *list_headers]
    else:
        list_row_widths = list_widths
        list_header_labels = list_headers

    st.markdown(
        f"<div style='text-align:right; opacity:0.72; font-size:0.85rem; "
        f"margin:0.1rem 0 0.4rem;'>Showing {start + 1}–"
        f"{start + len(page_df)} of {n}"
        f"{f' · Page {page} of {pages}' if pages > 1 else ''}</div>",
        unsafe_allow_html=True
    )

    if view == "Cards":
        for i in range(0, len(page_df), per_row):
            cols = st.columns(per_row)
            chunk = page_df.iloc[i:i + per_row]

            for col, (_, job) in zip(cols, chunk.iterrows()):
                with col:
                    if bulk_mode:
                        render_bulk_checkbox(
                            job,
                            selection_key,
                            generation
                        )
                    render_card(job)

    else:
        with st.container(key=f"{key}_list_view"):
            header = st.columns(list_row_widths, vertical_alignment="center")
            for col, label in zip(header, list_header_labels):
                col.caption(f"**{label}**")

            for _, job in page_df.iterrows():
                with st.container(
                    key=f"{key}_list_item_{job['job_id']}"
                ):
                    row = st.columns(
                        list_row_widths,
                        vertical_alignment="center",
                        gap="small"
                    )
                    if bulk_mode:
                        with row[0]:
                            render_bulk_checkbox(
                                job,
                                selection_key,
                                generation
                            )
                        render_row(job, row[1:])
                    else:
                        render_row(job, row)


# --------------------------------------------------
# ADD A JOB MANUALLY
# --------------------------------------------------

def find_existing_job(url, exclude_job_id=None):
    """Return (title, company) if this link is already in the database."""

    url = (url or "").strip()

    if not url:
        return None

    linkedin_id = linkedin_importer.linkedin_job_id(url) or ""

    conn = get_connection()
    query = """
        SELECT title, company FROM jobs
        WHERE (job_url = ? OR job_id = ?)
    """
    params = [url, linkedin_id]
    if exclude_job_id:
        query += " AND job_id != ?"
        params.append(str(exclude_job_id))

    row = conn.execute(query, params).fetchone()
    conn.close()

    return row


def add_manual_job(
    title,
    company,
    location,
    url,
    employment_type,
    weekly_hours,
    description,
    track,
    status,
    date_posted=None
):
    """Save a job I found myself, score it and (optionally) set its status."""

    # Imported here so a missing scorer gives a clear error before anything
    # is written to the database.
    from job_scorer.scorer import calculate_scores

    job_id = (
        linkedin_importer.linkedin_job_id(url)
        or f"manual-{uuid4().hex[:12]}"
    )

    job = {
        "job_id": job_id,
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "employment_type": employment_type,
        "weekly_hours": weekly_hours,
    }
    job_scores = calculate_scores(job)

    conn = get_connection()

    conn.execute("""
        INSERT INTO jobs (
            job_id, title, company, location, job_url, source,
            description, employment_type, weekly_hours,
            description_status, manual_track, date_posted
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)
    """, (
        job_id, title, company, location, url or None, MANUAL_SOURCE,
        description, employment_type, weekly_hours, track, date_posted
    ))

    # Score the job for every track, the same way scorer.py does.
    for track_code in TRACK_OPTIONS.values():
        score, reasons = job_scores[track_code]

        conn.execute("""
            INSERT INTO job_scores (job_id, track, score, reasons)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id, track)
            DO UPDATE SET
                score = excluded.score,
                reasons = excluded.reasons,
                scored_at = CURRENT_TIMESTAMP
        """, (job_id, track_code, score, json.dumps(reasons)))

    conn.commit()
    conn.close()

    if status != "New":
        update_job_status(job_id, status)

    return job_id


def load_job_for_edit(job_id):
    """Load editable fields and tracking details for one job."""

    conn = get_connection()
    row = conn.execute("""
        SELECT
            j.job_id, j.title, j.company, j.location, j.job_url,
            j.date_posted, j.employment_type, j.weekly_hours,
            j.description, j.source, j.collected_at AS added_at,
            j.archived_at,
            COALESCE(t.status, 'New') AS status,
            COALESCE(t.application_date, '') AS application_date,
            COALESCE(t.stage, 'Not started') AS stage,
            COALESCE(t.notes, '') AS notes
        FROM jobs j
        LEFT JOIN job_tracking t ON j.job_id = t.job_id
        WHERE j.job_id = ?
    """, (str(job_id),)).fetchone()
    scores = conn.execute("""
        SELECT track, score
        FROM job_scores
        WHERE job_id = ?
        ORDER BY track
    """, (str(job_id),)).fetchall()
    conn.close()

    if row is None:
        return None

    columns = [
        "job_id", "title", "company", "location", "job_url", "date_posted",
        "employment_type", "weekly_hours", "description", "source", "added_at",
        "archived_at", "status", "application_date", "stage", "notes"
    ]
    job = dict(zip(columns, row))
    job["scores"] = dict(scores)
    return job


def archive_job(job_id):
    """Hide a job from active lists while keeping its history."""

    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET archived_at = CURRENT_TIMESTAMP WHERE job_id = ?",
        (str(job_id),)
    )
    conn.commit()
    conn.close()


def restore_job(job_id):
    """Return an archived job to active lists."""

    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET archived_at = NULL WHERE job_id = ?",
        (str(job_id),)
    )
    conn.commit()
    conn.close()


def delete_job(job_id):
    """Permanently remove a job and prevent email sync from re-adding it."""

    conn = get_connection()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS deleted_jobs (
                    job_id TEXT PRIMARY KEY,
                    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            exists = conn.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?",
                (str(job_id),)
            ).fetchone()
            if not exists:
                raise ValueError(f"Job {job_id} no longer exists.")

            conn.execute(
                "INSERT OR IGNORE INTO deleted_jobs (job_id) VALUES (?)",
                (str(job_id),)
            )
            conn.execute(
                "DELETE FROM job_scores WHERE job_id = ?",
                (str(job_id),)
            )
            conn.execute(
                "DELETE FROM job_tracking WHERE job_id = ?",
                (str(job_id),)
            )
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (str(job_id),))
    finally:
        conn.close()


def archive_jobs(job_ids):
    """Archive a collection of jobs in one database transaction."""

    job_ids = [str(job_id) for job_id in job_ids]
    if not job_ids:
        raise ValueError("Select at least one job to archive.")

    conn = get_connection()
    try:
        with conn:
            existing_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT job_id FROM jobs WHERE job_id IN "
                    f"({','.join('?' for _ in job_ids)})",
                    job_ids
                )
            }
            missing_ids = set(job_ids) - existing_ids
            if missing_ids:
                raise ValueError(
                    "Some selected jobs no longer exist: "
                    + ", ".join(sorted(missing_ids))
                )
            conn.executemany(
                "UPDATE jobs SET archived_at = CURRENT_TIMESTAMP "
                "WHERE job_id = ?",
                [(str(job_id),) for job_id in job_ids]
            )
    finally:
        conn.close()


def load_expiration_check_jobs():
    """Return active jobs with valid LinkedIn job links."""

    conn = get_connection()
    rows = conn.execute("""
        SELECT job_id, title, company, job_url
        FROM jobs
        WHERE archived_at IS NULL
          AND expired_at IS NULL
          AND job_url IS NOT NULL
        ORDER BY collected_at DESC
    """).fetchall()
    conn.close()

    return [
        {
            "job_id": str(row[0]),
            "title": clean(row[1], "Untitled job"),
            "company": clean(row[2], "Unknown company"),
            "job_url": linkedin_importer.normalize_linkedin_url(row[3]),
        }
        for row in rows
        if linkedin_importer.linkedin_job_id(row[3])
    ]


def mark_jobs_expired(job_ids):
    """Persist confirmed expiration without changing application status."""

    if not job_ids:
        return

    conn = get_connection()
    try:
        with conn:
            conn.executemany("""
                UPDATE jobs
                SET expired_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
                  AND archived_at IS NULL
                  AND expired_at IS NULL
            """, [(str(job_id),) for job_id in job_ids])
    finally:
        conn.close()


def load_expired_jobs():
    """Return all expired jobs that have not yet been archived."""

    conn = get_connection()
    rows = conn.execute("""
        SELECT job_id, title, company, job_url, expired_at
        FROM jobs
        WHERE archived_at IS NULL
          AND expired_at IS NOT NULL
        ORDER BY expired_at DESC, title COLLATE NOCASE
    """).fetchall()
    conn.close()
    return [
        {
            "job_id": str(row[0]),
            "title": clean(row[1], "Untitled job"),
            "company": clean(row[2], "Unknown company"),
            "job_url": (
                linkedin_importer.normalize_linkedin_url(row[3]) or row[3]
            ),
            "expired_at": row[4],
        }
        for row in rows
    ]


def delete_jobs(job_ids):
    """Delete a collection of jobs atomically and add them to the sync blocklist."""

    job_ids = [str(job_id) for job_id in job_ids]
    if not job_ids:
        raise ValueError("Select at least one job to delete.")

    conn = get_connection()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS deleted_jobs (
                    job_id TEXT PRIMARY KEY,
                    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            existing_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT job_id FROM jobs WHERE job_id IN "
                    f"({','.join('?' for _ in job_ids)})",
                    job_ids
                )
            }
            missing_ids = set(job_ids) - existing_ids
            if missing_ids:
                raise ValueError(
                    "Some selected jobs no longer exist: "
                    + ", ".join(sorted(missing_ids))
                )

            conn.executemany(
                "INSERT OR IGNORE INTO deleted_jobs (job_id) VALUES (?)",
                [(job_id,) for job_id in job_ids]
            )
            for table in ("job_scores", "job_tracking", "jobs"):
                conn.executemany(
                    f"DELETE FROM {table} WHERE job_id = ?",
                    [(job_id,) for job_id in job_ids]
                )
    finally:
        conn.close()


@st.dialog("Delete job permanently?")
def confirm_delete_job_dialog(job_id, title):
    """Confirm permanent removal of a job and its tracking information."""

    st.warning(
        f"Delete **{title}** permanently? Its scores, application status and "
        "notes will also be deleted. This cannot be undone."
    )
    delete_col, cancel_col = st.columns(2)
    with delete_col:
        if st.button(
            "Delete permanently",
            type="primary",
            key=f"confirm_delete_{job_id}",
            **STRETCH
        ):
            try:
                delete_job(job_id)
            except ValueError as error:
                st.error(str(error))
                return
            st.toast("Job deleted.")
            st.rerun()

    with cancel_col:
        if st.button(
            "Cancel",
            key=f"cancel_delete_{job_id}",
            **STRETCH
        ):
            st.rerun()


@st.dialog("Delete selected jobs permanently?")
def confirm_bulk_delete_dialog(job_ids, selection_key, generation_key):
    """Confirm permanent removal of selected jobs and their tracking data."""

    st.warning(
        f"Delete {len(job_ids)} selected jobs permanently? Their scores, "
        "application statuses and notes will also be deleted. "
        "This cannot be undone."
    )
    delete_col, cancel_col = st.columns(2)
    with delete_col:
        if st.button(
            "Delete permanently",
            type="primary",
            key="confirm_bulk_delete",
            **STRETCH
        ):
            try:
                delete_jobs(job_ids)
            except ValueError as error:
                st.error(str(error))
                return
            st.session_state[selection_key] = []
            st.session_state[generation_key] += 1
            st.toast(f"Deleted {len(job_ids)} jobs.")
            st.rerun()

    with cancel_col:
        if st.button(
            "Cancel",
            key="cancel_bulk_delete",
            **STRETCH
        ):
            st.rerun()


def update_job_details(job_id, title, company, location, url, date_posted,
                       employment_type, weekly_hours, description):
    """Update job details and refresh its scores when scoring inputs change."""

    from job_scorer.scorer import calculate_scores

    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT title, location, description, employment_type,
                   weekly_hours
            FROM jobs
            WHERE job_id = ?
        """, (str(job_id),)).fetchone()

        if row is None:
            raise ValueError(f"Job {job_id} no longer exists.")

        old_title, old_location, old_description, old_type, old_hours = row
        score_inputs_changed = (
            title != old_title
            or location != old_location
            or description != (old_description or "")
            or employment_type != old_type
            or weekly_hours != old_hours
        )

        job_scores = None
        if score_inputs_changed:
            score_job = {
                "job_id": str(job_id),
                "title": title,
                "location": location,
                "description": description,
                "employment_type": employment_type,
                "weekly_hours": weekly_hours,
            }
            job_scores = calculate_scores(score_job)

        with conn:
            conn.execute("""
                UPDATE jobs
                SET title = ?,
                    company = ?,
                    location = ?,
                    job_url = ?,
                    date_posted = ?,
                    employment_type = ?,
                    weekly_hours = ?,
                    description = ?,
                    description_status = 'completed'
                WHERE job_id = ?
            """, (
                title,
                company,
                location,
                url or None,
                date_posted,
                employment_type,
                weekly_hours,
                description,
                str(job_id)
            ))

            if job_scores is not None:
                for track_code in TRACK_OPTIONS.values():
                    score, reasons = job_scores[track_code]
                    conn.execute("""
                        INSERT INTO job_scores (job_id, track, score, reasons)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(job_id, track)
                        DO UPDATE SET
                            score = excluded.score,
                            reasons = excluded.reasons,
                            scored_at = CURRENT_TIMESTAMP
                    """, (
                        str(job_id),
                        track_code,
                        score,
                        json.dumps(reasons)
                    ))
    finally:
        conn.close()

    return score_inputs_changed


def render_job_edit_form(job_id, job=None, form_key=None):
    """Render the shared job editor and save changes when submitted."""

    job = job or load_job_for_edit(job_id)
    if job is None:
        st.error("This job could not be found. It may have been removed.")
        return

    form_key = form_key or f"edit_job_form_{job_id}"
    st.caption(
        "Changing the title, location or description recalculates the match "
        "scores for every career track."
    )

    employment_options = EMPLOYMENT_TYPES
    current_employment = clean(job["employment_type"], "Not specified")
    if current_employment not in employment_options:
        current_employment = "Not specified"

    with st.form(key=form_key):
        title = st.text_input("Job title *", value=clean(job["title"]))
        company = st.text_input("Company *", value=clean(job["company"]))
        location = st.text_input(
            "Location",
            value=clean(job["location"]),
            placeholder="e.g. Hannover, Germany or Remote"
        )
        url = st.text_input("Job link", value=clean(job["job_url"]))
        posted_date = st.text_input(
            "Offer posted (YYYY-MM-DD)",
            value=normalize_posted_date(job["date_posted"]) or ""
        )
        col1, col2 = st.columns(2)
        with col1:
            employment = st.selectbox(
                "Employment type",
                employment_options,
                index=employment_options.index(current_employment)
            )
        with col2:
            weekly_hours = st.number_input(
                "Weekly hours (0 = unknown)",
                min_value=0.0,
                max_value=80.0,
                value=float(job["weekly_hours"] or 0),
                step=1.0
            )
        description = st.text_area(
            "Job description",
            value=clean(job["description"]),
            height=240
        )
        submitted = st.form_submit_button(
            "Save changes",
            type="primary",
            **STRETCH
        )

    if not submitted:
        return

    title = title.strip()
    company = company.strip()
    url = url.strip()
    description = description.strip()

    if not title or not company:
        st.error("Please enter at least a job title and a company.")
        return

    if url and not url.lower().startswith(("http://", "https://")):
        st.error("The job link should start with http:// or https://")
        return

    try:
        posted_date = parse_optional_posted_date(posted_date)
    except ValueError as error:
        st.error(str(error))
        return

    url = linkedin_importer.normalize_linkedin_url(url) or url
    existing = find_existing_job(url, exclude_job_id=job_id)
    if existing:
        st.error(
            f"This link belongs to another job in your database: "
            f"{existing[0]} at {clean(existing[1], 'unknown company')}."
        )
        return

    changed = update_job_details(
        job_id=job_id,
        title=title,
        company=company,
        location=location.strip() or None,
        url=url,
        date_posted=posted_date,
        employment_type=(
            None if employment == "Not specified" else employment
        ),
        weekly_hours=float(weekly_hours) if weekly_hours > 0 else None,
        description=description
    )

    if changed:
        st.toast("Job updated and match scores recalculated.")
    else:
        st.toast("Job updated.")
    if form_key.startswith("job_details_edit_form_"):
        st.session_state[f"job_details_edit_{job_id}"] = False
    st.rerun()


@st.dialog("Edit job offer", width="large")
def edit_job_dialog(job_id):
    """Edit a job and refresh match scores when its scoring inputs change."""

    render_job_edit_form(job_id)


@st.dialog("Job details", width="medium")
def job_details_dialog(job_id):
    """Show a job's full details, notes and management actions."""

    job = load_job_for_edit(job_id)
    if job is None:
        st.error("This job could not be found. It may have been removed.")
        return

    edit_key = f"job_details_edit_{job_id}"
    delete_key = f"job_details_delete_{job_id}"

    if st.session_state.get(edit_key):
        st.button(
            "Cancel editing",
            key=f"job_details_back_{job_id}",
            on_click=set_session_state_value,
            args=(edit_key, False)
        )
        render_job_edit_form(
            job_id,
            job=job,
            form_key=f"job_details_edit_form_{job_id}"
        )
        return

    st.subheader(job["title"])
    st.caption(clean(job["company"], "Company not specified"))

    left_col, right_col = st.columns(2)
    with left_col:
        st.caption(
            f"📍 {clean(job['location'], 'Not specified')} · "
            f"💼 {clean(job['employment_type'], 'Not specified')} · "
            f"⏱ {job['weekly_hours']:g} h/week"
            if pd.notna(job["weekly_hours"])
            else (
                f"📍 {clean(job['location'], 'Not specified')} · "
                f"💼 {clean(job['employment_type'], 'Not specified')} · "
                "⏱ Hours not specified"
            )
        )
        st.caption(
            f"📅 Posted {format_display_date(job['date_posted'])} · "
            f"🗓 Added {format_display_date(job['added_at'])}"
        )
    with right_col:
        st.caption(
            f"{STATUS_ICONS.get(job['status'], '')} {job['status']} · "
            f"🧭 {job['stage']}"
        )
        st.caption(
            f"📩 Applied {format_display_date(job['application_date'])} · "
            f"Source: {clean(job['source'], 'Not specified')}"
        )
        if job["archived_at"]:
            st.caption(f"📦 Archived {job['archived_at']}")

    if job["scores"]:
        score_labels = {
            code: label for label, code in TRACK_OPTIONS.items()
        }
        st.caption(" · ".join(
            f"{score_labels.get(track, track)} {score:.0f}%"
            for track, score in job["scores"].items()
        ))

    if clean(job["job_url"]):
        st.link_button(
            "Open original job",
            job["job_url"],
            **STRETCH
        )

    st.markdown("#### Job description")
    description = clean(job["description"])
    expanded_key = f"job_details_show_more_{job_id}"
    expanded = st.session_state.get(expanded_key, False)
    preview_description = description_preview(description)
    visible_description = description if expanded else preview_description
    st.markdown(format_job_description(visible_description))
    if len(description) > len(preview_description):
        st.button(
            "Show less" if expanded else "Show more",
            key=f"job_details_description_toggle_{job_id}",
            on_click=set_session_state_value,
            args=(expanded_key, not expanded)
        )

    with st.expander("Notes", expanded=bool(clean_notes(job["notes"]))):
        with st.form(key=f"job_details_notes_form_{job_id}"):
            notes = st.text_area(
                "Notes",
                value=clean_notes(job["notes"]),
                placeholder="Add your notes about this job or application..."
            )
            save_notes = st.form_submit_button(
                "Save notes",
                **STRETCH
            )

        if save_notes:
            update_job_notes(job_id, notes)
            st.toast("Notes saved.")

    if st.session_state.get(delete_key):
        st.warning(
            "Permanently delete this job? Its scores, application status "
            "and notes will also be deleted. This cannot be undone."
        )
        confirm_col, cancel_col = st.columns(2)
        with confirm_col:
            if st.button(
                "Delete permanently",
                type="primary",
                key=f"job_details_confirm_delete_{job_id}",
                **STRETCH
            ):
                try:
                    delete_job(job_id)
                except ValueError as error:
                    st.error(str(error))
                    return
                st.session_state.pop(delete_key, None)
                st.toast("Job deleted.")
                st.rerun()
        with cancel_col:
            st.button(
                "Cancel",
                key=f"job_details_cancel_delete_{job_id}",
                on_click=set_session_state_value,
                args=(delete_key, False),
                **STRETCH
            )
    else:
        edit_col, delete_col = st.columns(2)
        with edit_col:
            st.button(
                "✏️ Edit offer",
                key=f"job_details_edit_button_{job_id}",
                on_click=set_session_state_value,
                args=(edit_key, True),
                **STRETCH
            )
        with delete_col:
            st.button(
                "🗑️ Delete job",
                key=f"job_details_delete_button_{job_id}",
                on_click=set_session_state_value,
                args=(delete_key, True),
                **STRETCH
            )


# --------------------------------------------------
# LOAD JOBS
# --------------------------------------------------

def load_jobs(track=None, archived=False):
    """Load all matching jobs for a track with their saved tracking info.

    With no track, return each job once using its highest-scoring track.
    Archived jobs are returned only when archived=True.
    Status and stage filtering happens in pandas (see apply_filters), so
    changing a filter does not need a new database query.
    """

    conn = get_connection()

    query = """
        SELECT
            j.job_id,
            j.title,
            j.company,
            j.location,
            j.description,
            j.employment_type,
            j.weekly_hours,
            j.job_url,
            j.source,
            j.collected_at AS added_at,
            j.date_posted,
            j.expired_at,
            s.track,
            s.score,
            COALESCE(t.status, 'New') AS status,
            COALESCE(t.application_date, '') AS application_date,
            COALESCE(t.stage, 'Not started') AS stage,
            COALESCE(t.notes, '') AS notes

        FROM jobs j

        JOIN job_scores s
            ON j.job_id = s.job_id

        LEFT JOIN job_tracking t
            ON j.job_id = t.job_id

        WHERE j.description_status = 'completed'
          AND j.archived_at IS {archive_state}
          {track_filter}
          AND (
              s.score > 0
              OR (j.source = 'Manual' AND j.manual_track = s.track)
          )

        ORDER BY s.score DESC
    """

    track_filter = "AND s.track = ?" if track else ""
    archive_state = "NOT NULL" if archived else "NULL"
    query = query.format(
        track_filter=track_filter,
        archive_state=archive_state
    )
    params = (track,) if track else ()
    df = pd.read_sql_query(query, conn, params=params)

    conn.close()

    if not track:
        df = df.drop_duplicates(subset="job_id", keep="first")

    # collected_at is stored in UTC; show it in the computer's local time.
    df["added_at"] = pd.to_datetime(
        df["added_at"], format="mixed", utc=True, errors="coerce"
    ).dt.tz_convert(datetime.now().astimezone().tzinfo)

    return df


# --------------------------------------------------
# LOAD APPLICATION TRACKER
# --------------------------------------------------

def load_tracker():
    """Load saved, applied and rejected jobs."""

    conn = get_connection()

    query = """
        SELECT
            j.job_id,
            j.title,
            j.company,
            j.location,
            j.job_url,
            j.source,
            j.expired_at,
            t.status,
            t.application_date,
            t.stage,
            t.notes,
            t.updated_at

        FROM jobs j

        JOIN job_tracking t
            ON j.job_id = t.job_id

        WHERE t.status IN ('Saved', 'Applied', 'Rejected')
          AND j.archived_at IS NULL

        ORDER BY t.updated_at DESC
    """

    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


def jobs_with_application_dates(tracker_df):
    """Return tracked jobs that have a valid application date, newest first."""

    dated_jobs = tracker_df.copy()
    dated_jobs["_application_date"] = pd.to_datetime(
        dated_jobs["application_date"],
        errors="coerce"
    )
    dated_jobs = dated_jobs[dated_jobs["_application_date"].notna()]
    return dated_jobs.sort_values("_application_date", ascending=False)


# --------------------------------------------------
# APPLICATION PIPELINE
# --------------------------------------------------

PIPELINE_STAGES = [
    "Applied",
    "HR screening",
    "Interview",
    "Technical interview",
    "Final interview",
    "Offer",
    "Hired",
    "Rejected"
]


def load_pipeline():
    """
    Count active applications by recruitment stage.
    Rejected jobs are included as a separate pipeline stage.
    """

    conn = get_connection()

    query = """
        SELECT
            CASE
                WHEN t.status = 'Rejected' THEN 'Rejected'
                WHEN t.stage IS NULL OR t.stage = '' OR t.stage = 'Not started'
                    THEN 'Applied'
                ELSE t.stage
            END AS stage,
            COUNT(*) AS applications
        FROM job_tracking t
        JOIN jobs j ON j.job_id = t.job_id
        WHERE t.status IN ('Applied', 'Rejected')
          AND j.archived_at IS NULL
        GROUP BY
            CASE
                WHEN t.status = 'Rejected' THEN 'Rejected'
                WHEN t.stage IS NULL OR t.stage = '' OR t.stage = 'Not started'
                    THEN 'Applied'
                ELSE t.stage
            END
    """

    df = pd.read_sql_query(query, conn)
    conn.close()

    # Keep a consistent stage order and show zero-count stages too.
    counts = dict(zip(df["stage"], df["applications"]))

    return pd.DataFrame({
        "Stage": PIPELINE_STAGES,
        "Applications": [
            int(counts.get(stage, 0))
            for stage in PIPELINE_STAGES
        ]
    })


# --------------------------------------------------
# DATE FORMATTING
# --------------------------------------------------

def format_display_date(value):
    """Format stored dates for display."""

    if pd.isna(value) or not value:
        return "Not available"

    try:
        return pd.to_datetime(value).strftime("%d %b %Y")
    except (ValueError, TypeError):
        return str(value)


def normalize_posted_date(value):
    """Return a date-like value as YYYY-MM-DD, or None if invalid."""

    text = str(value or "").strip()
    if not text:
        return None

    try:
        if "T" in text or " " in text:
            return datetime.fromisoformat(
                text.replace("Z", "+00:00")
            ).date().isoformat()
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def parse_optional_posted_date(value):
    """Validate an optional YYYY-MM-DD date entered in a form."""

    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError("Use YYYY-MM-DD for the offer posted date.") from error


# --------------------------------------------------
# DAILY SYNC STATUS (written by run_daily.py)
# --------------------------------------------------

def load_sync_status():
    """Return (latest run, latest successful run) from the sync_runs table."""

    conn = get_connection()

    try:
        latest = conn.execute("""
            SELECT finished_at, status, new_jobs, message
            FROM sync_runs ORDER BY id DESC LIMIT 1
        """).fetchone()

        success = conn.execute("""
            SELECT finished_at, new_jobs
            FROM sync_runs WHERE status = 'success'
            ORDER BY id DESC LIMIT 1
        """).fetchone()

    except sqlite3.OperationalError:
        # The table appears after the first run of run_daily.py.
        latest, success = None, None

    finally:
        conn.close()

    return latest, success


def run_daily_sync_button():
    if not st.button(
        "🔄 Sync",
        key="run_daily_sync",
        help="Run the daily job collection and scoring pipeline now"
    ):
        return

    try:
        with st.spinner("Running the daily job sync..."):
            result = subprocess.run(
                [
                    sys.executable,
                    str(BASE_DIR / "run_daily.py"),
                    "--force"
                ],
                cwd=BASE_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace"
            )
    except OSError as error:
        st.error(f"Could not start the daily sync: {error}")
        return

    if result.returncode:
        st.error(f"Daily sync failed with exit code {result.returncode}.")
        output = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part and part.strip()
        )
        if output:
            with st.expander("Sync output"):
                st.code(output[-12000:])
    else:
        st.toast("Daily sync completed.")
        st.rerun()


@st.dialog("LinkedIn expiration check", width="large")
def show_expired_jobs_dialog():
    """Show confirmed expired jobs and offer to archive them together."""

    result = st.session_state.get("expiration_check_result", {})
    expired_jobs = result.get("expired_jobs", [])
    unknown_jobs = result.get("unknown_jobs", [])
    checked_count = result.get("checked_count", 0)

    if expired_jobs:
        st.write(
            f"**{len(expired_jobs)} job"
            f"{'s' if len(expired_jobs) != 1 else ''}** marked as expired "
            "and awaiting archive."
        )
        st.caption(
            f"Checked {checked_count} LinkedIn job"
            f"{'s' if checked_count != 1 else ''} in this run."
        )
        st.write("Would you like to archive these jobs?")

        for job in expired_jobs:
            title_col, link_col = st.columns([5, 1])
            title_col.write(f"**{job['title']}** — {job['company']}")
            with link_col:
                if job["job_url"]:
                    st.link_button("Open ↗", job["job_url"], **STRETCH)
    else:
        st.success(
            f"No expired jobs found among {checked_count} LinkedIn "
            f"job{'s' if checked_count != 1 else ''} checked."
        )

    if unknown_jobs:
        with st.expander(
            f"{len(unknown_jobs)} job"
            f"{'s' if len(unknown_jobs) != 1 else ''} could not be verified"
        ):
            for job in unknown_jobs:
                st.write(
                    f"**{job['title']}** — {job['company']}: {job['reason']}"
                )

    archive_col, close_col = st.columns(2)
    with archive_col:
        if expired_jobs and st.button(
            f"📦 Archive all {len(expired_jobs)} expired jobs",
            key="archive_all_expired_jobs",
            type="primary",
            **STRETCH
        ):
            try:
                archive_jobs([job["job_id"] for job in expired_jobs])
            except (ValueError, sqlite3.Error) as error:
                st.error(str(error))
            else:
                st.session_state.pop("expiration_check_result", None)
                st.toast(f"Archived {len(expired_jobs)} expired jobs.")
                st.rerun()

    with close_col:
        if st.button("Not now", key="close_expired_jobs_dialog", **STRETCH):
            st.session_state.pop("expiration_check_result", None)
            st.rerun()


def run_expiration_check_button():
    if not st.button(
        "⏰ Check LinkedIn",
        key="check_linkedin_expiration",
        help="Check active LinkedIn jobs for expired listings"
    ):
        return

    try:
        jobs = load_expiration_check_jobs()
    except sqlite3.Error as error:
        st.error(f"Could not load LinkedIn jobs to check: {error}")
        return

    progress = st.progress(0.0)
    progress_text = st.empty()

    def update_progress(current, total):
        progress.progress(current / total if total else 1.0)
        progress_text.caption(f"Checking LinkedIn jobs: {current} of {total}")

    try:
        with st.spinner("Checking job listings on LinkedIn..."):
            results = check_linkedin_jobs(jobs, on_progress=update_progress)
            expired_ids = [
                result.job_id
                for result in results
                if result.status == "expired"
            ]
            mark_jobs_expired(expired_ids)
            expired_jobs = load_expired_jobs()
    except (OSError, sqlite3.Error) as error:
        st.error(f"Could not complete the LinkedIn expiration check: {error}")
        return

    unknown_jobs = [
        {
            "title": result.title,
            "company": result.company,
            "reason": result.reason,
        }
        for result in results
        if result.status == "unknown"
    ]
    st.session_state["expiration_check_result"] = {
        "checked_count": len(jobs),
        "expired_jobs": expired_jobs,
        "unknown_jobs": unknown_jobs,
    }
    st.rerun()


def show_sync_status():
    """One line about the last sync, plus a warning if something is wrong."""

    latest, success = load_sync_status()
    sync_col, expiration_col, status_col = st.columns(
        [1.2, 1.8, 7],
        vertical_alignment="center"
    )

    if success is None:
        status_col.caption("🔄 No daily sync recorded yet.")

    else:
        local_tz = datetime.now().astimezone().tzinfo
        finished = pd.to_datetime(success[0], utc=True).tz_convert(local_tz)
        new_jobs = int(success[1] or 0)

        status_col.caption(
            f"🔄 Last successful sync: {finished:%d %b %Y, %H:%M} · "
            f"{new_jobs} new job{'s' if new_jobs != 1 else ''}"
        )

        age = pd.Timestamp.now(tz=local_tz) - finished

        if age > pd.Timedelta(hours=36):
            status_col.warning(
                "No successful sync for more than a day. "
                "Check that the scheduled task is running."
            )

    with sync_col:
        run_daily_sync_button()
    with expiration_col:
        run_expiration_check_button()

    if latest is not None and latest[1] == "failed":
        status_col.warning(f"The most recent sync failed: {latest[3]}")


# --------------------------------------------------
# INITIALIZE DATABASE
# --------------------------------------------------

def check_database():
    """Show a clear message instead of a traceback if the database is wrong.

    It also avoids silently creating a new, empty database when the file
    is missing (sqlite3 would otherwise do that).
    """

    if not DB_PATH.exists():
        st.error(f"Database file not found:\n\n`{DB_PATH}`")
        st.stop()

    conn = sqlite3.connect(f"{DB_PATH.as_uri()}?mode=ro", uri=True)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    conn.close()

    if not {"jobs", "job_scores"} <= tables:
        st.error(
            f"The database `{DB_PATH}` ({DB_PATH.stat().st_size:,} bytes) "
            "has no `jobs` / `job_scores` table. It is empty or it is not "
            "your job database. Restore your real database into this "
            "location, then reload the page."
        )
        st.stop()


check_database()
create_tracking_table()
ensure_manual_job_column()


# --------------------------------------------------
# MAIN DASHBOARD
# --------------------------------------------------

st.title("💼 AI Job Assistant")

st.caption(
    "Find, evaluate and track job opportunities matching your career profile."
)

show_sync_status()

if st.session_state.get("expiration_check_result") is not None:
    show_expired_jobs_dialog()

st.divider()

tab_jobs, tab_best_matches, tab_archived, tab_tracker = st.tabs([
    "🔎 Find Jobs",
    "⭐ Best Matches",
    "📦 Archived Jobs",
    "📋 Application Tracker"
])


# ==================================================
# TAB 1: FIND JOBS
# ==================================================

def job_meta_lines(job):
    """Compact text lines with every detail of a job (used in card view)."""

    location = clean(job["location"], "Not specified")
    employment = clean(job["employment_type"], "Not specified")

    hours = (
        f"{job['weekly_hours']:g} h/week"
        if pd.notna(job["weekly_hours"])
        else "hours not specified"
    )

    lines = [
        f"📍 {location}",
        f"💼 {employment} · ⏱ {hours}",
        f"📅 Posted {format_display_date(job['date_posted'])}",
        f"🗓️ Added {format_added(job['added_at'])}",
    ]

    if clean(job.get("expired_at")):
        lines.append(
            f"⏰ Expired {format_display_date(job['expired_at'])}"
        )

    status_line = (
        f"{STATUS_ICONS.get(job['status'], '')} **{job['status']}**"
        f" · 🧭 {job['stage']}"
    )

    if clean(job["application_date"]):
        status_line += (
            f" · 📩 {format_display_date(job['application_date'])}"
        )

    lines.append(status_line)
    return lines


def job_edit_button(job, column, key_prefix="jobs", icon_only=False):
    """Open the edit dialog for a job."""

    with column:
        if st.button(
            "✏️" if icon_only else "✏️ Edit offer",
            key=f"{key_prefix}_edit_{job['job_id']}",
            help="Edit job offer",
            **STRETCH
        ):
            edit_job_dialog(job["job_id"])


def job_title_button(job, column=None, key_prefix="jobs"):
    """Make the job header the primary link to its details dialog."""

    container = column if column is not None else st.container()
    with container:
        if st.button(
            job["title"],
            key=f"{key_prefix}_details_{job['job_id']}",
            help=clean(job["title"], "View job details"),
            type="tertiary",
            **STRETCH
        ):
            job_details_dialog(job["job_id"])
        st.caption(company_line(job))


def job_action_buttons(
    job, link_col, save_col, reject_col, applied_col, reset_col,
    key_prefix="jobs"
):
    """Link, Save / Reject / Applied and Reset-to-New buttons."""

    with link_col:
        if clean(job["job_url"]):
            st.link_button(
                "🔗" if key_prefix == "tracker" else "↗",
                job["job_url"],
                help="View original job",
                **STRETCH
            )

    with save_col:
        if st.button(
            "⭐",
            key=f"{key_prefix}_save_{job['job_id']}",
            help="Save",
            **STRETCH
        ):
            update_job_status(job["job_id"], "Saved")
            st.rerun()

    with reject_col:
        if st.button(
            "❌",
            key=f"{key_prefix}_reject_{job['job_id']}",
            help="Reject",
            **STRETCH
        ):
            update_job_status(job["job_id"], "Rejected")
            st.rerun()

    with applied_col:
        if st.button(
            "📩",
            key=f"{key_prefix}_applied_{job['job_id']}",
            help="Mark as applied",
            **STRETCH
        ):
            update_job_status(job["job_id"], "Applied")
            st.rerun()

    with reset_col:
        if job["status"] != "New" and st.button(
            "↩️",
            key=f"{key_prefix}_reset_{job['job_id']}",
            help="Clear status and mark as new again",
            **STRETCH
        ):
            confirm_reset_dialog(job["job_id"], job["title"])

def job_lifecycle_actions(
    job,
    archive_col,
    delete_col,
    archived=False,
    key_prefix="jobs",
    edit_col=None
):
    """Archive or restore a job, or ask before permanently deleting it."""

    if edit_col is not None:
        job_edit_button(
            job,
            edit_col,
            key_prefix=key_prefix,
            icon_only=True
        )

    with archive_col:
        if archived:
            if st.button(
                "↩️",
                key=f"{key_prefix}_restore_{job['job_id']}",
                help="Restore job to active lists",
                **STRETCH
            ):
                restore_job(job["job_id"])
                st.toast("Job restored.")
                st.rerun()
        elif st.button(
            "📦",
            key=f"{key_prefix}_archive_{job['job_id']}",
            help="Archive job",
            **STRETCH
        ):
            archive_job(job["job_id"])
            st.toast("Job archived. You can find it in Archived Jobs.")
            st.rerun()

    with delete_col:
        if st.button(
            "🗑️",
            key=f"{key_prefix}_delete_{job['job_id']}",
            help="Delete job permanently",
            **STRETCH
        ):
            confirm_delete_job_dialog(job["job_id"], job["title"])


def render_job_card(job, key_prefix="jobs", archived=False):
    """Compact job card that still shows all information."""

    status_key = clean(job["status"], "New").lower().replace(" ", "_")
    card_key = f"{key_prefix}_job_card_{status_key}_{job['job_id']}"
    with st.container(border=True, key=card_key):

        title_col, score_col = st.columns([5, 1])

        job_title_button(job, title_col, key_prefix=key_prefix)
        score_col.markdown(f"**{score_text(job['score'])}**")

        st.caption("  \n".join(job_meta_lines(job)))

        notes = clean_notes(job["notes"])
        if notes:
            with st.expander("Notes"):
                st.write(notes)

        if archived:
            lifecycle_cols = st.columns(3)
            job_lifecycle_actions(
                job,
                lifecycle_cols[1],
                lifecycle_cols[2],
                archived=True,
                key_prefix=key_prefix,
                edit_col=lifecycle_cols[0]
            )
        else:
            job_action_buttons(
                job,
                *st.columns(5, gap="small"),
                key_prefix=key_prefix
            )
            lifecycle_cols = st.columns(3, gap="small")
            job_lifecycle_actions(
                job,
                lifecycle_cols[1],
                lifecycle_cols[2],
                key_prefix=key_prefix,
                edit_col=lifecycle_cols[0]
            )


def render_job_row(job, cols, key_prefix="jobs", archived=False):
    """One job as a single row in list view."""

    with cols[0].container(key=f"{key_prefix}_row_title_{job['job_id']}"):
        job_title_button(job, key_prefix=key_prefix)
    cols[1].write(clean(job["location"], "Not specified"))
    status_text = f"{STATUS_ICONS.get(job['status'], '')} {job['status']}"
    if clean(job.get("expired_at")):
        status_text += " · ⏰ Expired"
    cols[2].write(status_text)
    cols[3].write(job["stage"])
    cols[4].write(format_added(job["added_at"]))
    cols[5].write(
        format_display_date(job["application_date"])
        if clean(job["application_date"])
        else "–"
    )
    cols[6].write(f"**{score_text(job['score'])}**")

    with cols[7].container(key=f"{key_prefix}_row_actions_{job['job_id']}"):
        _, actions = st.columns([0.3, 1], gap=None)
        if archived:
            action_cols = actions.columns(4, gap=None)
            job_edit_button(
                job,
                action_cols[0],
                key_prefix=key_prefix,
                icon_only=True
            )
            with action_cols[1]:
                if clean(job["job_url"]):
                    st.link_button(
                        "🔗",
                        job["job_url"],
                        help="View original job",
                        **STRETCH
                    )
            job_lifecycle_actions(
                job,
                action_cols[2],
                action_cols[3],
                archived=True,
                key_prefix=key_prefix
            )
        else:
            action_cols = actions.columns(8, gap=None)
            job_edit_button(
                job,
                action_cols[0],
                key_prefix=key_prefix,
                icon_only=True
            )
            job_action_buttons(
                job,
                *action_cols[1:6],
                key_prefix=key_prefix
            )
            job_lifecycle_actions(
                job,
                action_cols[6],
                action_cols[7],
                key_prefix=key_prefix
            )


JOB_LIST_WIDTHS = [
    3.8, 1.5, 1.1, 1.2, 1, 1.1, 0.65, 4.65
]
JOB_LIST_HEADERS = [
    "Job", "Location", "Status", "Stage", "Added", "Applied on",
    "Match", ""
]


@st.dialog("Add a job manually")
def add_job_dialog(default_track_label):
    """Import a public job offer page or enter a job manually."""

    st.caption(
        "Paste a public job offer link to import its details, or enter them "
        "manually. A full description improves the match score."
    )

    link_col, fetch_col = st.columns([4, 1], vertical_alignment="bottom")
    with link_col:
        url = st.text_input(
            "Job link",
            key="manual_job_link",
            placeholder="https://example.com/careers/job-offer"
        )
    with fetch_col:
        fetch_clicked = st.button(
            "Fetch details",
            key="fetch_job_offer",
            **STRETCH
        )

    if fetch_clicked:
        try:
            imported = linkedin_importer.fetch_job_offer(url)
        except linkedin_importer.LinkedInImportError as error:
            st.error(str(error))
        else:
            st.session_state["manual_job_title"] = imported["title"]
            st.session_state["manual_job_company"] = imported["company"]
            st.session_state["manual_job_location"] = imported["location"]
            st.session_state["manual_job_description"] = imported["description"]
            st.session_state["manual_job_posted_date_text"] = (
                normalize_posted_date(imported["date_posted"]) or ""
            )
            imported_employment = imported["employment_type"]
            if imported_employment in EMPLOYMENT_TYPES:
                st.session_state["manual_job_employment_type"] = (
                    imported_employment
                )
            st.success(
                "Public job details fetched. Review and edit them before adding."
            )

    with st.form("manual_job_form"):

        title = st.text_input("Job title *", key="manual_job_title")
        company = st.text_input("Company *", key="manual_job_company")
        location = st.text_input(
            "Location",
            placeholder="e.g. Hannover, Germany or Remote",
            key="manual_job_location"
        )
        posted_date = st.text_input(
            "Offer posted (YYYY-MM-DD)",
            key="manual_job_posted_date_text",
            placeholder="e.g. 2026-11-16"
        )

        col1, col2 = st.columns(2)

        with col1:
            employment = st.selectbox(
                "Employment type",
                EMPLOYMENT_TYPES,
                key="manual_job_employment_type"
            )

        with col2:
            hours = st.number_input(
                "Weekly hours (0 = unknown)",
                min_value=0.0,
                max_value=80.0,
                value=0.0,
                step=1.0
            )

        col3, col4 = st.columns(2)

        track_labels = list(TRACK_OPTIONS.keys())

        with col3:
            track_label = st.selectbox(
                "Career track",
                track_labels,
                index=track_labels.index(default_track_label)
            )

        with col4:
            status = st.selectbox("Status", ["New", "Saved", "Applied"])

        description = st.text_area(
            "Job description",
            height=180,
            key="manual_job_description"
        )

        submitted = st.form_submit_button(
            "Add job",
            type="primary",
            **STRETCH
        )

    if not submitted:
        return

    title, company, url = title.strip(), company.strip(), url.strip()

    if not title or not company:
        st.error("Please enter at least a job title and a company.")
        return

    if url and not url.lower().startswith(("http://", "https://")):
        st.error("The job link should start with http:// or https://")
        return

    try:
        posted_date = parse_optional_posted_date(posted_date)
    except ValueError as error:
        st.error(str(error))
        return

    canonical_url = linkedin_importer.normalize_linkedin_url(url)
    if canonical_url:
        url = canonical_url

    existing = find_existing_job(url)

    if existing:
        st.error(
            f"This job is already in your database: "
            f"{existing[0]} at {clean(existing[1], 'unknown company')}."
        )
        return

    try:
        add_manual_job(
            title=title,
            company=company,
            location=location.strip() or None,
            url=url,
            employment_type=None if employment == "Not specified" else employment,
            weekly_hours=float(hours) if hours > 0 else None,
            description=description.strip(),
            track=TRACK_OPTIONS[track_label],
            status=status,
            date_posted=posted_date
        )

    except ImportError:
        st.error(
            "Could not load the scorer (job_scorer/scorer.py), "
            "so the job was not added."
        )
        return

    st.toast(f"Added “{title}” under {track_label}.")
    st.rerun()


with tab_jobs:

    track_options = TRACK_OPTIONS
    track_labels = ["All tracks", *track_options.keys()]

    heading_col, add_col = st.columns(
        [4, 1.5],
        vertical_alignment="center"
    )

    selected_track_heading = st.session_state.get(
        "jobs_track_filter",
        "All tracks"
    )
    heading_col.subheader(
        "All track opportunities"
        if selected_track_heading == "All tracks"
        else f"{selected_track_heading} opportunities"
    )

    if add_col.button(
        "➕ Add job manually",
        key="add_job_button",
        type="primary",
        **STRETCH
    ):
        default_track = (
            st.session_state.get("jobs_track_filter")
            if st.session_state.get("jobs_track_filter") in track_options
            else next(iter(track_options))
        )
        add_job_dialog(default_track)

    col1, col2, col3, col4, col5, col6 = st.columns(
        [1.6, 1.6, 1.6, 1.5, 1.5, 1.6]
    )

    with col1:
        selected_track = st.selectbox(
            "Choose career track",
            track_labels,
            index=0,
            key="jobs_track_filter"
        )

    with col2:
        search_query = st.text_input(
            "Search jobs",
            placeholder="Job title or company",
            key="jobs_search"
        )

    with col3:
        status_filter = st.multiselect(
            "Filter by status",
            list(STATUS_ICONS.keys()),
            placeholder="All statuses",
            key="jobs_status_filter"
        )

    with col4:
        stage_filter = st.multiselect(
            "Filter by stage",
            ALL_STAGES,
            placeholder="All stages",
            key="jobs_stage_filter"
        )

    with col5:
        added_filter = st.selectbox(
            "Added",
            list(ADDED_OPTIONS.keys()),
            key="jobs_added_filter"
        )

    with col6:
        sort_by = st.selectbox(
            "Sort by",
            ["Best match", "Newest first"],
            key="jobs_sort"
        )

    selected_track_code = track_options.get(selected_track)
    jobs = load_jobs(selected_track_code)
    jobs = apply_job_search(jobs, search_query)
    jobs = apply_filters(jobs, status_filter, stage_filter)
    jobs = apply_added_filter(jobs, added_filter)

    if sort_by == "Newest first":
        jobs = jobs.sort_values(
            ["added_at", "score"],
            ascending=[False, False]
        )

    if jobs.empty:
        st.info("No matching jobs found. Try changing the filters or search.")
    else:
        show_collection(
            jobs,
            key="jobs",
            render_card=render_job_card,
            render_row=render_job_row,
            list_widths=JOB_LIST_WIDTHS,
            list_headers=JOB_LIST_HEADERS,
            bulk_actions=True
        )


# ==================================================
# TAB 2: BEST MATCHES
# ==================================================

BEST_MATCH_LIMIT = 10

with tab_best_matches:

    st.subheader("⭐ Best matches by career track")
    st.caption(
        f"Top {BEST_MATCH_LIMIT} jobs with a positive match score for each "
        "track, ordered by score."
    )

    for track_label, track_code in TRACK_OPTIONS.items():
        st.subheader(track_label)
        best_jobs = load_jobs(track_code)
        best_jobs = best_jobs[best_jobs["score"] > 0].head(BEST_MATCH_LIMIT)

        if best_jobs.empty:
            st.info(f"No scored matches available for {track_label} yet.")
            continue

        show_collection(
            best_jobs,
            key=f"best_{track_code}",
            render_card=lambda job, prefix=f"best_{track_code}": render_job_card(
                job, key_prefix=prefix
            ),
            render_row=lambda job, cols, prefix=f"best_{track_code}": (
                render_job_row(job, cols, key_prefix=prefix)
            ),
            list_widths=JOB_LIST_WIDTHS,
            list_headers=JOB_LIST_HEADERS
        )


# ==================================================
# TAB 3: ARCHIVED JOBS
# ==================================================

with tab_archived:

    st.subheader("📦 Archived Jobs")
    st.caption(
        "Archived jobs are hidden from active lists and the application "
        "pipeline. Restore a job to make it active again, or delete it "
        "permanently."
    )

    archived_search = st.text_input(
        "Search archived jobs",
        placeholder="Job title or company",
        key="archived_jobs_search"
    )
    archived_jobs = load_jobs(archived=True)
    archived_jobs = apply_job_search(archived_jobs, archived_search)

    if archived_jobs.empty:
        st.info("No archived jobs match this search.")
    else:
        show_collection(
            archived_jobs,
            key="archived_jobs",
            render_card=lambda job: render_job_card(
                job,
                key_prefix="archived_jobs",
                archived=True
            ),
            render_row=lambda job, cols: render_job_row(
                job,
                cols,
                key_prefix="archived_jobs",
                archived=True
            ),
            list_widths=JOB_LIST_WIDTHS,
            list_headers=JOB_LIST_HEADERS
        )


# ==================================================
# TAB 4: APPLICATION TRACKER
# ==================================================

TRACKER_STATUSES = ["Saved", "Applied", "Rejected"]


def render_update_form(job, key_prefix="tracker"):
    """Form to update status, stage, date and notes of a tracked job."""

    current_stage = clean(job["stage"], "Not started")
    if current_stage not in ALL_STAGES:
        current_stage = "Not started"

    with st.form(key=f"{key_prefix}_{job['job_id']}"):

        status = st.selectbox(
            "Job status",
            TRACKER_STATUSES,
            index=TRACKER_STATUSES.index(job["status"])
        )

        stage = st.selectbox(
            "Recruitment stage",
            ALL_STAGES,
            index=ALL_STAGES.index(current_stage)
        )

        application_date = st.text_input(
            "Application date (YYYY-MM-DD)",
            value=clean(job["application_date"]),
            placeholder="e.g. 2026-11-15"
        )

        notes = st.text_area(
            "Notes",
            value=clean(job["notes"]),
            placeholder=(
                "Recruiter contact, interview feedback, next steps..."
            )
        )

        submitted = st.form_submit_button(
            "Save application details",
            **STRETCH
        )

        if submitted:

            date_value = application_date.strip()

            if date_value:
                try:
                    date_value = pd.to_datetime(
                        date_value,
                        format="%Y-%m-%d",
                        errors="raise"
                    ).strftime("%Y-%m-%d")

                except (ValueError, TypeError):
                    st.error(
                        "Please enter a valid date in YYYY-MM-DD format."
                    )
                    return
            else:
                date_value = None

            update_application_details(
                job["job_id"],
                status,
                date_value,
                stage,
                notes
            )

            st.toast("Application details saved!")
            st.rerun()


def render_tracker_card(job, key_prefix="tracker"):
    """Compact tracker card with all details."""

    status_key = clean(job["status"], "New").lower().replace(" ", "_")
    card_suffix = (
        str(job["job_id"])
        if key_prefix == "tracker"
        else f"{key_prefix}_{job['job_id']}"
    )
    with st.container(
        border=True,
        key=f"tracker_job_card_{status_key}_{card_suffix}"
    ):

        job_title_button(job, key_prefix=key_prefix)

        lines = [
            f"📍 {clean(job['location'], 'Not specified')}",
            (
                f"{STATUS_ICONS.get(job['status'], '')} **{job['status']}**"
                f" · 🧭 {job['stage']}"
            ),
            f"📩 Applied on {format_display_date(job['application_date'])}"
        ]
        if clean(job.get("expired_at")):
            lines.append(
                f"⏰ Expired {format_display_date(job['expired_at'])}"
            )
        st.caption("  \n".join(lines))

        if clean_notes(job["notes"]):
            with st.expander("Notes"):
                st.write(clean_notes(job["notes"]))

        with st.expander("Update application details"):
            render_update_form(job, key_prefix=key_prefix)

        edit_col, link_col, reset_col, archive_col, delete_col = st.columns(5)

        job_edit_button(job, edit_col, key_prefix=key_prefix)

        with link_col:
            if clean(job["job_url"]):
                st.link_button(
                    "🔗 Job" if key_prefix == "tracker" else "🔗",
                    job["job_url"],
                    **STRETCH
                )

        with reset_col:
            if st.button(
                "↩️ New",
                key=f"{key_prefix}_reset_{job['job_id']}",
                help="Clear status and mark as new again",
                **STRETCH
            ):
                confirm_reset_dialog(job["job_id"], job["title"])

        job_lifecycle_actions(
            job,
            archive_col,
            delete_col,
            key_prefix=key_prefix
        )


def render_tracker_row(job, cols, key_prefix="tracker"):
    """One tracked job as a single row in list view."""

    with cols[0].container(key=f"{key_prefix}_row_title_{job['job_id']}"):
        job_title_button(job, key_prefix=key_prefix)
    cols[1].write(clean(job["location"], "Not specified"))
    status_text = f"{STATUS_ICONS.get(job['status'], '')} {job['status']}"
    if clean(job.get("expired_at")):
        status_text += " · ⏰ Expired"
    cols[2].write(status_text)
    cols[3].write(clean(job["stage"], "–"))
    cols[4].write(format_display_date(job["application_date"]))

    with cols[5].container(key=f"{key_prefix}_row_actions_{job['job_id']}"):
        _, actions = st.columns([0.3, 1], gap=None)
        action_cols = actions.columns(6, gap=None)

        with action_cols[0]:
            if clean_notes(job["notes"]):
                with st.popover(
                    "📝",
                    help="Notes",
                    key=f"{key_prefix}_notes_{job['job_id']}",
                    **STRETCH
                ):
                    st.write(clean_notes(job["notes"]))

        with action_cols[1]:
            with st.popover(
                "✏️",
                help="Update application details",
                key=f"{key_prefix}_update_{job['job_id']}",
                **STRETCH
            ):
                render_update_form(job, key_prefix=key_prefix)

        with action_cols[2]:
            if clean(job["job_url"]):
                st.link_button(
                    "🔗",
                    job["job_url"],
                    help="View original job",
                    **STRETCH
                )

        with action_cols[3]:
            if st.button(
                "↩️",
                key=f"{key_prefix}_reset_{job['job_id']}",
                help="Clear status and mark as new again",
                **STRETCH
            ):
                confirm_reset_dialog(job["job_id"], job["title"])

        job_lifecycle_actions(
            job,
            action_cols[4],
            action_cols[5],
            key_prefix=key_prefix
        )

TRACKER_LIST_WIDTHS = [
    3.8, 1.5, 1.1, 1.4, 1.2, 4.6
]
TRACKER_LIST_HEADERS = [
    "Job", "Location", "Status", "Stage", "Applied on", ""
]


with tab_tracker:

    st.subheader("📋 Application Tracker")

    st.caption(
        "Keep track of saved jobs, application dates and recruitment progress."
    )

    tracker_df = load_tracker()

    # --------------------------------------------------
    # PIPELINE CHART
    # --------------------------------------------------

    st.subheader("Application Pipeline")

    pipeline_df = load_pipeline()

    total_pipeline = int(pipeline_df["Applications"].sum())

    if total_pipeline == 0:
        st.info("Your application pipeline is empty.")
    else:
        st.bar_chart(
            pipeline_df.set_index("Stage"),
            y="Applications"
        )

        st.dataframe(
            pipeline_df,
            hide_index=True,
            **STRETCH
        )

    st.divider()

    # --------------------------------------------------
    # SUMMARY METRICS
    # --------------------------------------------------

    total_saved = int((tracker_df["status"] == "Saved").sum())
    total_applied = int((tracker_df["status"] == "Applied").sum())
    total_rejected = int((tracker_df["status"] == "Rejected").sum())

    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric("⭐ Saved jobs", total_saved)

    with col2:
        st.metric("📩 Applications", total_applied)

    with col3:
        st.metric("❌ Rejected", total_rejected)

    st.divider()

    # --------------------------------------------------
    # APPLICATIONS BY MONTH
    # --------------------------------------------------

    st.subheader("Applications by month")

    dated_applications = jobs_with_application_dates(tracker_df)

    if dated_applications.empty:
        st.info(
            "No application dates recorded yet. Add an application date "
            "to a tracked job to see it grouped here."
        )
    else:
        month_options = (
            dated_applications["_application_date"]
            .dt.to_period("M")
            .astype(str)
            .drop_duplicates()
            .tolist()
        )

        selected_month = st.selectbox(
            "Application month",
            month_options,
            format_func=lambda month: pd.Period(month).strftime("%B %Y"),
            key="tracker_application_month"
        )

        month_applications = dated_applications[
            dated_applications["_application_date"]
            .dt.to_period("M")
            .astype(str)
            .eq(selected_month)
        ].drop(columns="_application_date")

        st.caption(
            f"{len(month_applications)} job"
            f"{'' if len(month_applications) == 1 else 's'} with an "
            "application date in this month. Jobs remain listed here if "
            "their status later changes."
        )

        show_collection(
            month_applications,
            key=f"tracker_month_{selected_month}",
            render_card=lambda job, prefix=f"tracker_month_{selected_month}": (
                render_tracker_card(job, key_prefix=prefix)
            ),
            render_row=lambda job, cols, prefix=f"tracker_month_{selected_month}": (
                render_tracker_row(job, cols, key_prefix=prefix)
            ),
            list_widths=TRACKER_LIST_WIDTHS,
            list_headers=TRACKER_LIST_HEADERS
        )

    st.divider()

    # --------------------------------------------------
    # TRACKED JOBS
    # --------------------------------------------------

    if tracker_df.empty:

        st.info(
            "You haven't saved or applied to any jobs yet. "
            "Start by saving a job in the Find Jobs tab."
        )

    else:

        col1, col2 = st.columns(2)

        with col1:
            tracker_status_filter = st.multiselect(
                "Filter by status",
                TRACKER_STATUSES,
                placeholder="All statuses",
                key="tracker_status_filter"
            )

        with col2:
            tracker_stage_filter = st.multiselect(
                "Filter by stage",
                ALL_STAGES,
                placeholder="All stages",
                key="tracker_stage_filter"
            )

        tracker_df = apply_filters(
            tracker_df,
            tracker_status_filter,
            tracker_stage_filter
        )

        if tracker_df.empty:
            st.info("No jobs found for these filters.")

        else:
            show_collection(
                tracker_df,
                key="tracker",
                render_card=render_tracker_card,
                render_row=render_tracker_row,
                list_widths=TRACKER_LIST_WIDTHS,
                list_headers=TRACKER_LIST_HEADERS
            )


# --------------------------------------------------
# FOOTER
# --------------------------------------------------

st.divider()

st.caption(
    "AI Job Assistant | Local SQLite database | "
    "Your job tracking information is stored locally."
)
