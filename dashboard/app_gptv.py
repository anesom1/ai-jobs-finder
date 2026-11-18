import sqlite3
from pathlib import Path
from datetime import date

import pandas as pd
import streamlit as st


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "jobs.db"

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


# --------------------------------------------------
# LOAD JOBS
# --------------------------------------------------

def load_jobs(track, status_filter):
    """Load matching jobs and their saved tracking information."""

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
          AND s.track = ?
          AND s.score > 0
          AND (? = 'All' OR COALESCE(t.status, 'New') = ?)

        ORDER BY s.score DESC
    """

    df = pd.read_sql_query(
        query,
        conn,
        params=(track, status_filter, status_filter)
    )

    conn.close()
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
            t.status,
            t.application_date,
            t.stage,
            t.notes,
            t.updated_at

        FROM jobs j

        JOIN job_tracking t
            ON j.job_id = t.job_id

        WHERE t.status IN ('Saved', 'Applied', 'Rejected')

        ORDER BY t.updated_at DESC
    """

    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


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
                WHEN status = 'Rejected' THEN 'Rejected'
                WHEN stage IS NULL OR stage = '' OR stage = 'Not started'
                    THEN 'Applied'
                ELSE stage
            END AS stage,
            COUNT(*) AS applications
        FROM job_tracking
        WHERE status IN ('Applied', 'Rejected')
        GROUP BY
            CASE
                WHEN status = 'Rejected' THEN 'Rejected'
                WHEN stage IS NULL OR stage = '' OR stage = 'Not started'
                    THEN 'Applied'
                ELSE stage
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


# --------------------------------------------------
# INITIALIZE DATABASE
# --------------------------------------------------

create_tracking_table()


# --------------------------------------------------
# MAIN DASHBOARD
# --------------------------------------------------

st.title("💼 AI Job Assistant")

st.caption(
    "Find, evaluate and track job opportunities matching your career profile."
)

st.divider()

tab_jobs, tab_tracker = st.tabs([
    "🔎 Find Jobs",
    "📋 Application Tracker"
])


# ==================================================
# TAB 1: FIND JOBS
# ==================================================

with tab_jobs:

    track_options = {
        "QA / Software Testing": "qa",
        "AI / Machine Learning": "ai",
        "AI Testing": "ai_testing"
    }

    col1, col2 = st.columns(2)

    with col1:
        selected_track = st.selectbox(
            "Choose career track",
            list(track_options.keys())
        )

    with col2:
        status_filter = st.selectbox(
            "Filter by status",
            ["All", "New", "Saved", "Applied", "Rejected"]
        )

    selected_track_code = track_options[selected_track]

    jobs = load_jobs(
        selected_track_code,
        status_filter
    )

    st.subheader(f"{selected_track} opportunities")
    st.write(f"Found **{len(jobs)}** matching jobs.")

    if jobs.empty:
        st.info("No matching jobs found for these filters.")

    else:
        for _, job in jobs.iterrows():

            with st.container(border=True):

                col1, col2 = st.columns([4, 1])

                with col1:
                    st.subheader(job["title"])
                    st.write(f"**{job['company']}**")

                with col2:
                    st.metric(
                        label="Match score",
                        value=f"{job['score']:.1f}%"
                    )

                location = (
                    job["location"]
                    if pd.notna(job["location"]) and job["location"]
                    else "Not specified"
                )
                st.write(f"📍 **Location:** {location}")

                employment = (
                    job["employment_type"]
                    if pd.notna(job["employment_type"])
                    else "Not specified"
                )

                hours = (
                    job["weekly_hours"]
                    if pd.notna(job["weekly_hours"])
                    else "Not specified"
                )

                st.caption(
                    f"Employment: {employment} | Weekly hours: {hours}"
                )

                # Show the application date only when one exists.
                if job["application_date"]:
                    applied_date = format_display_date(
                        job["application_date"]
                    )
                    st.caption(f"📩 **Applied on:** {applied_date}")

                st.caption(f"📍 **Recruitment stage:** {job['stage']}")

                with st.expander("Read job description"):
                    description = job["description"]

                    if pd.notna(description) and description:
                        st.write(description)
                    else:
                        st.write("No description available.")

                if pd.notna(job["job_url"]) and job["job_url"]:
                    st.link_button(
                        "View original job",
                        job["job_url"]
                    )

                st.write(f"**Status:** {job['status']}")

                col1, col2, col3 = st.columns(3)

                with col1:
                    if st.button(
                        "⭐ Save",
                        key=f"save_{job['job_id']}",
                        use_container_width=True
                    ):
                        update_job_status(job["job_id"], "Saved")
                        st.rerun()

                with col2:
                    if st.button(
                        "❌ Reject",
                        key=f"reject_{job['job_id']}",
                        use_container_width=True
                    ):
                        update_job_status(job["job_id"], "Rejected")
                        st.rerun()

                with col3:
                    if st.button(
                        "📩 Applied",
                        key=f"applied_{job['job_id']}",
                        use_container_width=True
                    ):
                        update_job_status(job["job_id"], "Applied")
                        st.rerun()


# ==================================================
# TAB 2: APPLICATION TRACKER
# ==================================================

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
            use_container_width=True
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
    # TRACKED JOBS
    # --------------------------------------------------

    if tracker_df.empty:

        st.info(
            "You haven't saved or applied to any jobs yet. "
            "Start by saving a job in the Find Jobs tab."
        )

    else:

        tracker_filter = st.selectbox(
            "Show jobs",
            ["All", "Saved", "Applied", "Rejected"],
            key="tracker_filter"
        )

        if tracker_filter != "All":
            tracker_df = tracker_df[
                tracker_df["status"] == tracker_filter
            ]

        if tracker_df.empty:
            st.info("No jobs found for this status.")

        else:

            stages = [
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

            for _, job in tracker_df.iterrows():

                with st.container(border=True):

                    st.subheader(job["title"])
                    st.write(f"**{job['company']}**")

                    location = (
                        job["location"]
                        if pd.notna(job["location"]) and job["location"]
                        else "Not specified"
                    )
                    st.caption(f"📍 {location}")

                    applied_date = format_display_date(
                        job["application_date"]
                    )

                    col1, col2 = st.columns(2)

                    with col1:
                        st.write(f"**Status:** {job['status']}")

                    with col2:
                        st.write(f"**Applied on:** {applied_date}")

                    st.write(f"**Recruitment stage:** {job['stage']}")

                    if pd.notna(job["job_url"]) and job["job_url"]:
                        st.link_button(
                            "View original job",
                            job["job_url"]
                        )

                    if job["notes"]:
                        with st.expander("Notes"):
                            st.write(job["notes"])

                    with st.expander("Update application details"):

                        current_stage = job["stage"]

                        if current_stage not in stages:
                            current_stage = "Not started"

                        current_status = job["status"]

                        with st.form(
                            key=f"tracker_{job['job_id']}"
                        ):

                            status = st.selectbox(
                                "Job status",
                                ["Saved", "Applied", "Rejected"],
                                index=[
                                    "Saved",
                                    "Applied",
                                    "Rejected"
                                ].index(current_status)
                            )

                            stage = st.selectbox(
                                "Recruitment stage",
                                stages,
                                index=stages.index(current_stage)
                            )

                            application_date = st.text_input(
                                "Application date (YYYY-MM-DD)",
                                value=job["application_date"] or "",
                                placeholder="e.g. 2026-11-15"
                            )

                            notes = st.text_area(
                                "Notes",
                                value=job["notes"] or "",
                                placeholder=(
                                    "Recruiter contact, interview feedback, "
                                    "next steps..."
                                )
                            )

                            submitted = st.form_submit_button(
                                "Save application details",
                                use_container_width=True
                            )

                            if submitted:

                                date_value = application_date.strip()

                                if date_value:
                                    try:
                                        parsed_date = pd.to_datetime(
                                            date_value,
                                            format="%Y-%m-%d",
                                            errors="raise"
                                        )
                                        date_value = parsed_date.strftime(
                                            "%Y-%m-%d"
                                        )

                                    except (ValueError, TypeError):
                                        st.error(
                                            "Please enter a valid date "
                                            "in YYYY-MM-DD format."
                                        )
                                        st.stop()
                                else:
                                    date_value = None

                                update_application_details(
                                    job["job_id"],
                                    status,
                                    date_value,
                                    stage,
                                    notes
                                )

                                st.success("Application details saved!")
                                st.rerun()


# --------------------------------------------------
# FOOTER
# --------------------------------------------------

st.divider()

st.caption(
    "AI Job Assistant | Local SQLite database | "
    "Your job tracking information is stored locally."
)
