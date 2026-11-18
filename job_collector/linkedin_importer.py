import json
import re
from datetime import date, datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; JobAssistant/1.0; "
        "+https://www.linkedin.com/)"
    )
}


class LinkedInImportError(Exception):
    """Raised when a LinkedIn job page cannot be imported."""


def normalize_date_posted(value):
    """Convert an ISO-8601 LinkedIn posting date to YYYY-MM-DD."""

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


def linkedin_job_id(url):
    """Extract the numeric ID from a LinkedIn job URL."""

    parsed = urlparse((url or "").strip())
    hostname = (parsed.hostname or "").lower()

    if hostname != "linkedin.com" and not hostname.endswith(".linkedin.com"):
        return None

    match = re.search(
        r"/(?:comm/)?jobs/view/(?:[^/?#]*?-)?(\d{6,})",
        parsed.path,
        re.IGNORECASE
    )
    return match.group(1) if match else None


def normalize_linkedin_url(url):
    """Return a canonical LinkedIn job URL, or None for other links."""

    job_id = linkedin_job_id(url)
    if not job_id:
        return None

    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def _job_posting_from_json_ld(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue

            graph = candidate.get("@graph", [])
            candidates.extend(graph if isinstance(graph, list) else [graph])

            job_type = candidate.get("@type", [])
            if isinstance(job_type, str):
                job_type = [job_type]
            if "JobPosting" in job_type:
                return candidate

    return {}


def _text_from_html(value):
    if not value:
        return ""
    return BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)


def _location_from_posting(posting):
    locations = posting.get("jobLocation", [])
    if isinstance(locations, dict):
        locations = [locations]

    for location in locations:
        if not isinstance(location, dict):
            continue
        address = location.get("address", {})
        if not isinstance(address, dict):
            continue

        parts = [
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("addressCountry"),
        ]
        parts = [
            part.get("name", "") if isinstance(part, dict) else str(part or "")
            for part in parts
        ]
        return ", ".join(part for part in parts if part)

    return ""


def fetch_linkedin_job(url):
    """Fetch public job details from a LinkedIn job page."""

    canonical_url = normalize_linkedin_url(url)
    if not canonical_url:
        raise LinkedInImportError(
            "Enter a LinkedIn job URL containing /jobs/view/ and a job ID."
        )

    try:
        response = requests.get(
            canonical_url,
            headers=HEADERS,
            timeout=20,
            allow_redirects=True
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise LinkedInImportError(
            f"LinkedIn could not be reached or did not return this job "
            f"({error}). You can still enter the details manually."
        ) from error

    response_host = (urlparse(response.url).hostname or "").lower()
    if (
        response_host != "linkedin.com"
        and not response_host.endswith(".linkedin.com")
    ):
        raise LinkedInImportError(
            "LinkedIn redirected this link to a different website. "
            "Check the link and enter the details manually if needed."
        )

    soup = BeautifulSoup(response.text, "html.parser")
    posting = _job_posting_from_json_ld(soup)

    title = str(posting.get("title") or "").strip()
    organization = posting.get("hiringOrganization", {})
    company = (
        str(organization.get("name") or "").strip()
        if isinstance(organization, dict)
        else ""
    )
    location = _location_from_posting(posting)
    description = _text_from_html(posting.get("description"))
    date_posted = normalize_date_posted(posting.get("datePosted"))

    if not title or not company:
        og_title = soup.find("meta", property="og:title")
        page_title = og_title.get("content", "") if og_title else ""
        parts = [part.strip() for part in page_title.split("|")]
        if len(parts) >= 2:
            title = title or parts[0]
            company = company or parts[1]

    if not description:
        description_element = soup.find(
            "div",
            class_="show-more-less-html__markup"
        )
        if description_element:
            description = description_element.get_text("\n", strip=True)

    if not title or not company:
        raise LinkedInImportError(
            "LinkedIn did not provide the job title and company. The listing "
            "may require a login or may no longer be available; enter the "
            "details manually."
        )

    return {
        "url": canonical_url,
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "date_posted": date_posted,
    }
