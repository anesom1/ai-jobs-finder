"""Check public LinkedIn job pages for clear signs that a listing expired."""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from job_collector.linkedin_importer import (
    HEADERS,
    linkedin_job_id,
    normalize_linkedin_url,
)


REQUEST_DELAY = 1
REQUEST_TIMEOUT = 20
EXPIRED_PHRASES = (
    "this job is no longer available",
    "job is no longer available",
    "this job has expired",
    "job has expired",
    "no longer accepting applications",
    "position has been filled",
    "this job is closed",
    "applications are closed",
)


@dataclass(frozen=True)
class ExpirationResult:
    job_id: str
    title: str
    company: str
    url: str
    status: str
    reason: str = ""


def _is_linkedin_host(url):
    hostname = (urlparse(url).hostname or "").lower()
    return hostname == "linkedin.com" or hostname.endswith(".linkedin.com")


def _job_posting_from_json_ld(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        candidates = data if isinstance(data, list) else [data]
        while candidates:
            candidate = candidates.pop()
            if not isinstance(candidate, dict):
                continue

            job_type = candidate.get("@type", [])
            if isinstance(job_type, str):
                job_type = [job_type]
            elif not isinstance(job_type, list):
                job_type = []
            if "JobPosting" in job_type:
                return candidate

            graph = candidate.get("@graph", [])
            if isinstance(graph, list):
                candidates.extend(graph)
            elif isinstance(graph, dict):
                candidates.append(graph)

    return {}


def check_linkedin_job(session, job):
    """Return expired, active, or unknown without guessing on blocked pages."""

    url = normalize_linkedin_url(job["job_url"])
    job_id = str(job["job_id"])
    if not url:
        return ExpirationResult(
            job_id, job["title"], job["company"], job["job_url"],
            "unknown", "Not a LinkedIn job URL."
        )

    try:
        response = session.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )
    except requests.RequestException as error:
        return ExpirationResult(
            job_id, job["title"], job["company"], url,
            "unknown", f"LinkedIn could not be reached: {error}"
        )

    if not _is_linkedin_host(response.url):
        return ExpirationResult(
            job_id, job["title"], job["company"], url,
            "unknown", "LinkedIn redirected to a different website."
        )

    if response.status_code in (404, 410):
        return ExpirationResult(
            job_id, job["title"], job["company"], url,
            "expired", f"LinkedIn returned HTTP {response.status_code}."
        )

    if response.status_code != 200:
        return ExpirationResult(
            job_id, job["title"], job["company"], url,
            "unknown", f"LinkedIn returned HTTP {response.status_code}."
        )

    final_job_id = linkedin_job_id(response.url)
    if final_job_id and final_job_id != job_id:
        return ExpirationResult(
            job_id, job["title"], job["company"], url,
            "expired", "LinkedIn redirected to a different job."
        )
    final_path = urlparse(response.url).path.rstrip("/")
    if not final_job_id and (
        final_path == "/jobs"
        or final_path.startswith("/jobs/search")
        or final_path.startswith("/jobs/view")
    ):
        return ExpirationResult(
            job_id, job["title"], job["company"], url,
            "expired", "LinkedIn redirected away from this job listing."
        )

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = soup.get_text(" ", strip=True).casefold()
    metadata_text = " ".join(
        str(tag.get("content", ""))
        for tag in soup.find_all("meta")
    ).casefold()
    page_content = f"{page_text} {metadata_text}"

    if any(phrase in page_content for phrase in EXPIRED_PHRASES):
        return ExpirationResult(
            job_id, job["title"], job["company"], url,
            "expired", "LinkedIn says the listing is no longer available."
        )

    posting = _job_posting_from_json_ld(soup)
    valid_through = str(posting.get("validThrough") or "").strip()
    if valid_through:
        try:
            expiration_date = datetime.fromisoformat(
                valid_through.replace("Z", "+00:00")
            ).date()
        except ValueError:
            expiration_date = None
        if expiration_date and expiration_date < datetime.now(
            timezone.utc
        ).date():
            return ExpirationResult(
                job_id, job["title"], job["company"], url,
                "expired", "LinkedIn's job posting expiration date has passed."
            )

    page_title = soup.find("meta", property="og:title")
    page_title_text = str(
        page_title.get("content", "") if page_title else ""
    ).strip()
    has_job_title = bool(
        page_title_text
        and "linkedin" not in page_title_text.casefold()
    )
    if final_job_id == job_id and (posting or has_job_title):
        return ExpirationResult(
            job_id, job["title"], job["company"], url,
            "active", "The LinkedIn job page is available."
        )

    return ExpirationResult(
        job_id, job["title"], job["company"], url,
        "unknown", "LinkedIn did not provide enough information to verify it."
    )


def check_linkedin_jobs(jobs, on_progress=None):
    """Check jobs sequentially and report progress after each request."""

    results = []
    with requests.Session() as session:
        for index, job in enumerate(jobs, start=1):
            results.append(check_linkedin_job(session, job))
            if on_progress:
                on_progress(index, len(jobs))
            if index < len(jobs):
                time.sleep(REQUEST_DELAY)
    return results
