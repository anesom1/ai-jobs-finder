# AI Job Assistant

A local job-search assistant. It collects job-alert emails from Gmail, extracts LinkedIn listings, fetches public job descriptions, scores completed listings against three career tracks, and presents them in a Streamlit dashboard for searching and application tracking. Job offers from other public job boards can also be imported by pasting their link into the manual-add form.

The database is stored locally in SQLite at `data/jobs_before_manual.db`. The app connects to Gmail and public job-offer pages over the internet; optional scoring uses a local Ollama server.

---

## How it works

```
Gmail alerts ──► gmail_collector ──► job_extractor ──► description_collector ──► scorer ──► dashboard
 (last 14 days)   saves emails       extracts LinkedIn   fetches public text      scores each   search, status,
                  to job_emails      listings            from LinkedIn            job per track archive, tracker
```

| Step | Script | What it does |
|------|--------|--------------|
| 1 | `email_collector/gmail_collector.py` | Searches the last 14 days of Gmail for LinkedIn and Indeed job-alert senders and saves new messages to `job_emails`. Previously saved messages are skipped. |
| 2 | `email_collector/job_extractor.py` | Extracts LinkedIn job IDs, titles, companies, locations and links from LinkedIn alert emails into `jobs`. It parses each listing separately and filters common LinkedIn interface labels so they are not saved as job details. On subsequent runs, it repairs existing LinkedIn rows with missing fields or recognized interface labels using the saved source email, updating only title, company and location. Indeed messages are collected but are not parsed into jobs. Deleted job IDs are skipped and duplicate jobs are ignored. |
| 3 | `job_collector/description_collector.py` | Fetches up to 25 pending or unverified LinkedIn descriptions per run, waits 2 seconds between requests, and checks company information before saving. |
| 4 | `job_scorer/scorer.py` | Scores jobs whose description status is `completed` for the three tracks and saves each score and its reasons in `job_scores`. |

`run_daily.py` runs these four steps in order. It retries description collection in batches until the queue is empty, the collector makes no progress, or it reaches eight batches. It is intended to be run daily.

### Career tracks and scoring

Tracks: `qa` (QA / software testing), `ai` (AI / machine learning) and `ai_testing` (testing AI systems).

A job gets a score from 0 to 100 made of these parts:

| Part | Points |
|------|--------|
| Skills found in the title and the responsibilities / requirements | 35 |
| Seniority (junior is best, senior is penalised) | 20 |
| Responsibilities | 15 |
| Location (Hannover and remote work from Germany / the EU score best) | 15 |
| Language (English-friendly is best, strong German requirement is penalised) | 5 |
| Working hours (neutral, full-time and part-time get the same) | 10 |

The scorer also asks a local Ollama model to assess semantic skill and role fit
for all eligible tracks in one request per job. The final score blends the
existing rule-based score (60%) with the LLM score (40%). Ollama results are
cached with the saved score and reused until the job title, description,
location, employment type, weekly hours, scoring prompt, or model changes.
If Ollama is unavailable or returns an invalid response, the scorer logs a
warning and uses the existing rule-based score.

For the `ai` and `ai_testing` tracks the job title (or, for `ai_testing`, the described duties) must clearly match the track. Otherwise the score is 0 and the job is not shown.

Install Ollama and pull the default model before scoring:

```bash
ollama pull qwen3:8b
```

`OLLAMA_MODEL` can select another installed model, and `OLLAMA_BASE_URL` can
point to a different Ollama server. For example, use `llama3.1:8b` on a
machine with less memory by setting `OLLAMA_MODEL=llama3.1:8b`. To update
scores for existing jobs, run `python job_scorer/scorer.py` or run the daily
pipeline with `python run_daily.py --force`.

### Description statuses

Jobs start with a pending description. The collector checks the company in LinkedIn's page title; if an "About The Company" section is present, it checks that too. A successful fetch is marked `completed`; an explicit company mismatch is marked `failed` or `mismatch`; a removed listing is marked `unavailable`. Fetch errors and short or incomplete pages are marked `failed`. The daily pipeline retries `pending` and `unverified` jobs; only jobs marked `completed` are scored and shown in the active and archived job collections.

---

## Project structure

```
ai-job-assistant/
├── run_daily.py                  # runs the whole pipeline (schedule this)
├── credentials.json              # Google OAuth client file (secret, not in git)
├── token.json                    # saved Gmail login (secret, not in git)
├── data/
│   └── jobs_before_manual.db     # SQLite database used by the dashboard and pipeline
├── logs/                         # sync logs (created automatically)
├── email_collector/
│   ├── gmail_collector.py        # step 1
│   └── job_extractor.py          # step 2
├── job_collector/
│   ├── description_collector.py  # step 3
│   ├── expiration_checker.py     # on-demand LinkedIn availability check
│   └── linkedin_importer.py      # public job-page importer and URL helpers
├── job_scorer/
│   └── scorer.py                 # step 4
├── dashboard/
│   └── app.py                    # Streamlit dashboard
└── .venv/                        # Python virtual environment
```

Other files in the project (helper scripts, older versions) are not part of the daily pipeline.

---

## Setup

### 1. Python environment

```bash
cd ai-job-assistant
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The dashboard needs **Streamlit 1.40 or newer** (it uses segmented controls and dialogs).

### 2. Gmail access

1. In Google Cloud Console, create a project, enable the **Gmail API** and create an **OAuth client ID** of type *Desktop app*.
2. Download the client file and save it as `credentials.json` in the project folder.
3. Log in once by hand. A browser window opens and `token.json` is created:

   ```bash
   python email_collector/gmail_collector.py
   ```

The app only asks for read-only access (`gmail.readonly`).

> **Important:** if the OAuth consent screen is in *Testing* status, Google expires the login after about 7 days and the daily sync will report "Gmail login is missing or expired". Set the publishing status to *In production* (OAuth consent screen settings) to avoid this.

### 3. Keep secrets out of git

Make sure `.gitignore` contains:

```
credentials.json
token.json
data/
logs/
.venv/
.env
__pycache__/
*.pyc
```

These are already covered by the project's `.gitignore`.

---

## Running it

Run everything from the project folder. The dashboard and daily pipeline use `data/jobs_before_manual.db`.

**Whole pipeline (recommended):**

```bash
python run_daily.py            # skipped if a sync finished less than 20 hours ago
python run_daily.py --force    # run anyway
```

`run_daily.py` stops at the first step that fails, writes a log to `logs/sync_YYYY-MM-DD.log` and records the result in the `sync_runs` table.

To run the offline regression tests for LinkedIn alert parsing:

```bash
python -m unittest test_job_extractor -v
```

**Single steps:**

```bash
python email_collector/gmail_collector.py
python email_collector/job_extractor.py
python job_collector/description_collector.py
python job_scorer/scorer.py
```

**Dashboard:**

```bash
streamlit run dashboard/app.py
```

---

## Optional daily schedule (macOS)

To schedule the sync with launchd, save the following as `~/Library/LaunchAgents/com.jobassistant.sync.plist`, replacing `/path/to/ai-job-assistant` with the project path, and create the `logs` folder first. The example runs at 08:00 and when you log in. `run_daily.py` skips a run if a successful sync finished less than 20 hours ago, unless `--force` is passed.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jobassistant.sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/ai-job-assistant/.venv/bin/python</string>
    <string>/path/to/ai-job-assistant/run_daily.py</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/ai-job-assistant</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/path/to/ai-job-assistant/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>/path/to/ai-job-assistant/logs/launchd.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.jobassistant.sync.plist     # switch on
launchctl start com.jobassistant.sync                                 # run now
launchctl unload ~/Library/LaunchAgents/com.jobassistant.sync.plist   # switch off
```

---

## Dashboard

**Header:** shows the time of the last successful sync and how many new jobs it found. A warning appears if the last sync failed or if there was no successful sync for more than a day.

Use **⏰ Check LinkedIn** to visit each active, unexpired LinkedIn job link and check whether it has expired. This on-demand check is not part of the daily sync and uses public pages without logging in. Confirmed expired jobs are marked separately from application status and listed in a modal with an **Archive all expired jobs** action. If LinkedIn blocks a request or the page does not clearly identify the job, it is listed as unverified rather than marked expired. Jobs without LinkedIn job links are skipped.

**🔎 Find Jobs**
- Choose a career track or select **All tracks** (the default) to see each job once, using its best track score.
- Search by job title or company name.
- Filter by status (New, Saved, Applied, Rejected), by recruitment stage and by when the job was added (Today, last 7 / 30 days). Sort by best match or newest first.
- Switch between **Cards** (compact grid, 2 to 5 per row) and **List** view. Results are paginated with Previous / Next buttons, a page number selector and adjustable page size.
- Cards use different background accents for New, Saved, Applied and Rejected jobs.
- Every job shows title, company, match score, location, employment type, weekly hours, status, stage, the date it was added and (if known) the application date. Confirmed expired offers also show an expiration marker.
- Select a job title in Cards or List view to open its details, including the description, edit or delete the offer, and add notes without changing its application status. This also works in Best Matches, Archived Jobs and the Application Tracker. The details dialog shows a short description preview by default; use **Show more** to expand the formatted sections and bullet lists.
- Cards show both the offer's posted date (when available from LinkedIn or entered manually) and the date it was added to the dashboard. Existing jobs without a known posted date show **Not available**.
- Select **✏️ Edit offer** on a job card or list row to update its details. Changing the title, location, employment type, weekly hours or description refreshes its scores for all career tracks; status and application-tracker data are preserved.
- Use **📦 Archive** to move an offer out of active lists and the application pipeline. Archived offers are available in the **📦 Archived Jobs** tab and can be restored.
- Use **Bulk actions** in Find Jobs to select visible search results and archive or permanently delete them together.
- Use **🗑️ Delete** to permanently remove an offer, scores and tracking details after confirmation. Deleted LinkedIn jobs are recorded so the daily email import does not add them again.
- Actions: ⭐ save, ❌ reject, 📩 mark as applied, ↩️ clear the status and mark as new again (asks for confirmation and removes the stage, application date and notes).
- **➕ Add job manually** opens a form for jobs that did not come from the email alerts (see below).

### Adding a job manually

Use **➕ Add job manually** to add a job I found myself. Paste a public job-offer link (for example, LinkedIn or a Recruitee listing) and select **Fetch details** to prefill the title, company, location, description and any available posted date or employment type. The importer reads standard JobPosting structured data when available and falls back to page headings and metadata. Review and edit the imported details before adding. Some sites may block automated requests or not expose all fields; fill in missing details manually.

- Required: job title and company. Optional: location, posted date (`YYYY-MM-DD`), link, employment type, weekly hours and description.
- I choose the career track it belongs to and its starting status (New, Saved or Applied).
- Paste the job description to get a meaningful match score. The job is scored for all tracks with the same scorer as the email jobs.
- LinkedIn job IDs are used as IDs in the database, so a later email sync will not add the same listing again.
- Manual jobs are marked **✍️ Manual** next to the company name, in Find Jobs and in the Application Tracker, in both Cards and List view.
- A job always shows up under the track it was added to, even if its score there is 0 (the score then appears as "–"). It also appears in other tracks where it scores above 0.
- If the link is already in the database (for example a LinkedIn job that arrived by email), the job is not added twice.
- The daily sync keeps manual jobs and re-scores them together with all other jobs.

**⭐ Best Matches**
- Shows the top 10 positively scored jobs for each career track, in score order.
- Uses the same job cards and application actions as Find Jobs.

**📋 Application Tracker**
- Pipeline chart for active applications and counters for saved, applied and rejected jobs.
- Applications can be viewed by month using their recorded application date; jobs remain included if their status later changes.
- Same status / stage filters and Cards / List toggle.
- Update status, recruitment stage, application date (`YYYY-MM-DD`) and notes for each job.

Recruitment stages: Not started, Applied, HR screening, Interview, Technical interview, Final interview, Offer, Hired, No response, Rejected.

---

## Database

| Table | Content |
|-------|---------|
| `job_emails` | Raw job-alert emails from Gmail |
| `jobs` | One row per job, including title, company, location, link, description, description status, source email, `date_posted`, `expired_at`, `archived_at` and `collected_at` (UTC). The dashboard adds missing `manual_track`, `date_posted`, `archived_at` and `expired_at` columns when it starts. |
| `job_scores` | Score and scoring reasons per job and track |
| `job_tracking` | My status, stage, application date and notes per job (jobs without a row count as *New*) |
| `deleted_jobs` | Job IDs that were permanently deleted, to prevent the email importer from re-adding them |
| `sync_runs` | One row per daily sync: start and finish times, status, number of new jobs and error message |

The dashboard and pipeline create or upgrade their tables and columns as needed. Back up `data/jobs_before_manual.db` before manually changing the database.

---

## Troubleshooting

| Problem | What to do |
|---------|------------|
| Dashboard says "Gmail login is missing or expired" | Run `python email_collector/gmail_collector.py` by hand and log in again. If this happens about every week, see the note about the OAuth consent screen above. |
| "No successful sync for more than a day" | Check `logs/launchd.log` and the latest file in `logs/`. Make sure the Mac was on and the launchd job is loaded. |
| `Operation not permitted` in `logs/launchd.log` | macOS blocks background access to the project folder. Move the project out of Documents / Desktop / Downloads, or give the `.venv` Python Full Disk Access. |
| Jobs stuck as `failed` after a bad network moment | Set them back to pending: `UPDATE jobs SET description_status = 'pending' WHERE description_status = 'failed';` |
| Nothing new appears | Run `python run_daily.py --force` and read the log. Only LinkedIn job-alert emails are turned into jobs. |

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
