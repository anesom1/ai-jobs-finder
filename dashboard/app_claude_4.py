import sqlite3
from pathlib import Path
from datetime import date, datetime

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

# `use_container_width` is deprecated in newer Streamlit versions and
# replaced by `width="stretch"`. Pick whichever the installed version knows.
_version = tuple(int(p) for p in st.__version__.split(".")[:2])
STRETCH = (
    {"width": "stretch"}
    if _version >= (1, 50)
    else {"use_container_width": True}
)


def clean(value, fallback=""):
    """Return a display string, or a fallback for None / NaN / empty."""

    if value is None or pd.isna(value) or str(value).strip() == "":
        return fallback

    return str(value)


def apply_filters(df, statuses, stages):
    """Filter by status and stage. An empty selection means 'no filter'."""

    if statuses:
        df = df[df["status"].isin(statuses)]

    if stages:
        df = df[df["stage"].isin(stages)]

    return df


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


def show_collection(
    df,
    key,
    render_card,
    render_row,
    list_widths,
    list_headers
):
    """
    Show a DataFrame of jobs either as a compact card grid or as a list,
    with a view switch and simple pagination.
    """

    n = len(df)

    c1, c2, c3, c4 = st.columns([2, 1.5, 1.5, 1.5])

    view = c1.segmented_control(
        "View",
        ["Cards", "List"],
        default="Cards",
        format_func=lambda v: {"Cards": "🗂️ Cards", "List": "📃 List"}[v],
        key=f"{key}_view",
        label_visibility="collapsed"
    ) or "Cards"

    per_row = 3
    if view == "Cards":
        per_row = c2.selectbox(
            "Cards per row",
            [2, 3, 4, 5],
            index=1,
            key=f"{key}_per_row"
        )

    page_size = c3.selectbox(
        "Per page",
        [12, 24, 48, 100],
        index=1,
        key=f"{key}_page_size"
    )

    pages = max(1, -(-n // page_size))

    page = 1
    if pages > 1:
        # The key includes the result count so the page resets to 1
        # whenever the filters change the number of results.
        page = c4.number_input(
            f"Page (of {pages})",
            min_value=1,
            max_value=pages,
            value=1,
            key=f"{key}_page_{n}_{page_size}"
        )

    start = (page - 1) * page_size
    page_df = df.iloc[start:start + page_size]

    st.caption(f"Showing {start + 1}–{start + len(page_df)} of {n}")

    if view == "Cards":
        for i in range(0, len(page_df), per_row):
            cols = st.columns(per_row)
            chunk = page_df.iloc[i:i + per_row]

            for col, (_, job) in zip(cols, chunk.iterrows()):
                with col:
                    render_card(job)

    else:
        header = st.columns(list_widths, vertical_alignment="center")
        for col, label in zip(header, list_headers):
            col.caption(f"**{label}**")

        for _, job in page_df.iterrows():
            st.markdown(
                "<hr style='margin: 0.25rem 0; opacity: 0.25;'>",
                unsafe_allow_html=True
            )
            row = st.columns(list_widths, vertical_alignment="center")
            render_row(job, row)


# --------------------------------------------------
# LOAD JOBS
# --------------------------------------------------

def load_jobs(track):
    """Load all matching jobs for a track with their saved tracking info.

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
            j.collected_at AS added_at,
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

        ORDER BY s.score DESC
    """

    df = pd.read_sql_query(query, conn, params=(track,))

    conn.close()

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


def show_sync_status():
    """One line about the last sync, plus a warning if something is wrong."""

    latest, success = load_sync_status()

    if success is None:
        st.caption("🔄 No daily sync recorded yet.")

    else:
        local_tz = datetime.now().astimezone().tzinfo
        finished = pd.to_datetime(success[0], utc=True).tz_convert(local_tz)
        new_jobs = int(success[1] or 0)

        st.caption(
            f"🔄 Last successful sync: {finished:%d %b %Y, %H:%M} · "
            f"{new_jobs} new job{'s' if new_jobs != 1 else ''}"
        )

        age = pd.Timestamp.now(tz=local_tz) - finished

        if age > pd.Timedelta(hours=36):
            st.warning(
                "No successful sync for more than a day. "
                "Check that the scheduled task is running."
            )

    if latest is not None and latest[1] == "failed":
        st.warning(f"The most recent sync failed: {latest[3]}")


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

show_sync_status()

st.divider()

tab_jobs, tab_tracker = st.tabs([
    "🔎 Find Jobs",
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
        f"🗓️ Added {format_added(job['added_at'])}",
    ]

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


def job_action_buttons(
    job, link_col, save_col, reject_col, applied_col, reset_col
):
    """Link, Save / Reject / Applied and Reset-to-New buttons."""

    with link_col:
        if clean(job["job_url"]):
            st.link_button(
                "🔗",
                job["job_url"],
                help="View original job",
                **STRETCH
            )

    with save_col:
        if st.button(
            "⭐",
            key=f"save_{job['job_id']}",
            help="Save",
            **STRETCH
        ):
            update_job_status(job["job_id"], "Saved")
            st.rerun()

    with reject_col:
        if st.button(
            "❌",
            key=f"reject_{job['job_id']}",
            help="Reject",
            **STRETCH
        ):
            update_job_status(job["job_id"], "Rejected")
            st.rerun()

    with applied_col:
        if st.button(
            "📩",
            key=f"applied_{job['job_id']}",
            help="Mark as applied",
            **STRETCH
        ):
            update_job_status(job["job_id"], "Applied")
            st.rerun()

    with reset_col:
        if job["status"] != "New" and st.button(
            "↩️",
            key=f"reset_{job['job_id']}",
            help="Clear status and mark as new again",
            **STRETCH
        ):
            confirm_reset_dialog(job["job_id"], job["title"])


def render_job_card(job):
    """Compact job card that still shows all information."""

    with st.container(border=True):

        title_col, score_col = st.columns([4, 1])

        title_col.markdown(f"**{job['title']}**  \n{clean(job['company'])}")
        score_col.markdown(f"**{job['score']:.0f}%**")

        st.caption("  \n".join(job_meta_lines(job)))

        with st.expander("Job description"):
            st.write(clean(job["description"], "No description available."))

        job_action_buttons(job, *st.columns(5))


def render_job_row(job, cols):
    """One job as a single row in list view."""

    cols[0].markdown(f"**{job['title']}**  \n{clean(job['company'])}")
    cols[1].write(clean(job["location"], "Not specified"))

    hours = (
        f"{job['weekly_hours']:g} h/week"
        if pd.notna(job["weekly_hours"])
        else "–"
    )
    cols[2].write(f"{clean(job['employment_type'], '–')} · {hours}")

    cols[3].write(f"{STATUS_ICONS.get(job['status'], '')} {job['status']}")
    cols[4].write(job["stage"])
    cols[5].write(format_added(job["added_at"]))
    cols[6].write(
        format_display_date(job["application_date"])
        if clean(job["application_date"])
        else "–"
    )
    cols[7].write(f"**{job['score']:.0f}%**")

    with cols[8]:
        with st.popover("📄", help="Read job description", **STRETCH):
            st.write(clean(job["description"], "No description available."))

    job_action_buttons(job, cols[9], cols[10], cols[11], cols[12], cols[13])


JOB_LIST_WIDTHS = [
    3, 1.8, 1.8, 1.1, 1.3, 1.1, 1.1, 0.7, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6
]
JOB_LIST_HEADERS = [
    "Job", "Location", "Employment", "Status", "Stage",
    "Added", "Applied on", "Match", "", "", "", "", "", ""
]


with tab_jobs:

    track_options = {
        "QA / Software Testing": "qa",
        "AI / Machine Learning": "ai",
        "AI Testing": "ai_testing"
    }

    col1, col2, col3, col4, col5 = st.columns([2, 2, 2, 1.5, 1.5])

    with col1:
        selected_track = st.selectbox(
            "Choose career track",
            list(track_options.keys())
        )

    with col2:
        status_filter = st.multiselect(
            "Filter by status",
            list(STATUS_ICONS.keys()),
            placeholder="All statuses",
            key="jobs_status_filter"
        )

    with col3:
        stage_filter = st.multiselect(
            "Filter by stage",
            ALL_STAGES,
            placeholder="All stages",
            key="jobs_stage_filter"
        )

    with col4:
        added_filter = st.selectbox(
            "Added",
            list(ADDED_OPTIONS.keys()),
            key="jobs_added_filter"
        )

    with col5:
        sort_by = st.selectbox(
            "Sort by",
            ["Best match", "Newest first"],
            key="jobs_sort"
        )

    jobs = load_jobs(track_options[selected_track])
    jobs = apply_filters(jobs, status_filter, stage_filter)
    jobs = apply_added_filter(jobs, added_filter)

    if sort_by == "Newest first":
        jobs = jobs.sort_values(
            ["added_at", "score"],
            ascending=[False, False]
        )

    st.subheader(f"{selected_track} opportunities")

    if jobs.empty:
        st.info("No matching jobs found for these filters.")
    else:
        show_collection(
            jobs,
            key="jobs",
            render_card=render_job_card,
            render_row=render_job_row,
            list_widths=JOB_LIST_WIDTHS,
            list_headers=JOB_LIST_HEADERS
        )


# ==================================================
# TAB 2: APPLICATION TRACKER
# ==================================================

TRACKER_STATUSES = ["Saved", "Applied", "Rejected"]


def render_update_form(job):
    """Form to update status, stage, date and notes of a tracked job."""

    current_stage = clean(job["stage"], "Not started")
    if current_stage not in ALL_STAGES:
        current_stage = "Not started"

    with st.form(key=f"tracker_{job['job_id']}"):

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


def render_tracker_card(job):
    """Compact tracker card with all details."""

    with st.container(border=True):

        st.markdown(f"**{job['title']}**  \n{clean(job['company'])}")

        lines = [
            f"📍 {clean(job['location'], 'Not specified')}",
            (
                f"{STATUS_ICONS.get(job['status'], '')} **{job['status']}**"
                f" · 🧭 {job['stage']}"
            ),
            f"📩 Applied on {format_display_date(job['application_date'])}"
        ]
        st.caption("  \n".join(lines))

        if clean(job["notes"]):
            with st.expander("Notes"):
                st.write(job["notes"])

        with st.expander("Update application details"):
            render_update_form(job)

        link_col, reset_col = st.columns(2)

        with link_col:
            if clean(job["job_url"]):
                st.link_button("🔗 Job", job["job_url"], **STRETCH)

        with reset_col:
            if st.button(
                "↩️ New",
                key=f"tracker_reset_{job['job_id']}",
                help="Clear status and mark as new again",
                **STRETCH
            ):
                confirm_reset_dialog(job["job_id"], job["title"])


def render_tracker_row(job, cols):
    """One tracked job as a single row in list view."""

    cols[0].markdown(f"**{job['title']}**  \n{clean(job['company'])}")
    cols[1].write(clean(job["location"], "Not specified"))
    cols[2].write(f"{STATUS_ICONS.get(job['status'], '')} {job['status']}")
    cols[3].write(clean(job["stage"], "–"))
    cols[4].write(format_display_date(job["application_date"]))

    with cols[5]:
        if clean(job["notes"]):
            with st.popover("📝", help="Notes", **STRETCH):
                st.write(job["notes"])

    with cols[6]:
        with st.popover("✏️", help="Update application details", **STRETCH):
            render_update_form(job)

    with cols[7]:
        if clean(job["job_url"]):
            st.link_button(
                "🔗",
                job["job_url"],
                help="View original job",
                **STRETCH
            )

    with cols[8]:
        if st.button(
            "↩️",
            key=f"tracker_reset_{job['job_id']}",
            help="Clear status and mark as new again",
            **STRETCH
        ):
            confirm_reset_dialog(job["job_id"], job["title"])


TRACKER_LIST_WIDTHS = [3, 1.8, 1.1, 1.4, 1.2, 0.6, 0.6, 0.6, 0.6]
TRACKER_LIST_HEADERS = [
    "Job", "Location", "Status", "Stage", "Applied on", "", "", "", ""
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
