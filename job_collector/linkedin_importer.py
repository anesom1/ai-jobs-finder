import ipaddress
import json
import re
import socket
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AIJobAssistant/1.0)"
}
MAX_REDIRECTS = 5
MAX_PAGE_BYTES = 5_000_000


class LinkedInImportError(Exception):
    """Raised when a public job offer page cannot be imported."""


JobImportError = LinkedInImportError


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
    pending = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        pending.extend(data if isinstance(data, list) else [data])

    while pending:
        candidate = pending.pop()
        if not isinstance(candidate, dict):
            continue

        job_type = candidate.get("@type", [])
        if isinstance(job_type, str):
            job_type = [job_type]
        elif not isinstance(job_type, list):
            job_type = []
        if any(
            str(value).rsplit("/", 1)[-1] == "JobPosting"
            for value in job_type
        ):
            return candidate

        for key in ("@graph", "mainEntity"):
            nested = candidate.get(key)
            if isinstance(nested, list):
                pending.extend(nested)
            elif isinstance(nested, dict):
                pending.append(nested)

    return {}


def _text_from_html(value):
    if not value:
        return ""
    return BeautifulSoup(str(value), "html.parser").get_text("\n", strip=True)


def _location_from_posting(posting):
    locations = posting.get("jobLocation", [])
    if isinstance(locations, dict):
        locations = [locations]
    if isinstance(locations, str):
        locations = [{"name": locations}]
    if not isinstance(locations, list):
        locations = []

    found = []
    for location in locations:
        if isinstance(location, str):
            value = location
        elif isinstance(location, dict):
            address = location.get("address", {})
            if not isinstance(address, dict):
                address = {}

            parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            ]
            parts = [
                part.get("name", "")
                if isinstance(part, dict)
                else str(part or "")
                for part in parts
            ]
            value = ", ".join(part for part in parts if part)
            if not value:
                name = location.get("name", "")
                value = (
                    name.get("name", "")
                    if isinstance(name, dict)
                    else str(name or "")
                )
        else:
            continue

        value = re.sub(r"\s+", " ", value).strip()
        if value and value not in found:
            found.append(value)

    location_type = posting.get("jobLocationType", "")
    if isinstance(location_type, list):
        location_type = " ".join(str(value) for value in location_type)
    if str(location_type).upper() == "TELECOMMUTE":
        if not any("remote" in value.casefold() for value in found):
            found.insert(0, "Remote")

    return ", ".join(found)


def _validate_public_url(url):
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise JobImportError(
            "Enter a public job offer link starting with http:// or https://."
        )

    try:
        addresses = {
            ipaddress.ip_address(result[4][0])
            for result in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM
            )
        }
    except (OSError, ValueError) as error:
        raise JobImportError(
            f"Could not resolve the job offer website: {error}"
        ) from error

    if not addresses or any(not address.is_global for address in addresses):
        raise JobImportError("The job link must point to a public website.")


def _fetch_public_page(url):
    try:
        with requests.Session() as session:
            current_url = url
            for redirect_count in range(MAX_REDIRECTS + 1):
                _validate_public_url(current_url)
                response = session.get(
                    current_url,
                    headers=HEADERS,
                    timeout=20,
                    allow_redirects=False,
                    stream=True
                )

                if response.is_redirect or response.is_permanent_redirect:
                    redirect_url = response.headers.get("Location")
                    response.close()
                    if not redirect_url:
                        raise JobImportError(
                            "The job offer page redirected without a destination."
                        )
                    if redirect_count == MAX_REDIRECTS:
                        raise JobImportError(
                            "The job offer page redirected too many times."
                        )
                    current_url = urljoin(current_url, redirect_url)
                    continue

                try:
                    response.raise_for_status()
                    content_type = response.headers.get(
                        "Content-Type", ""
                    ).casefold()
                    if content_type and not any(
                        kind in content_type
                        for kind in ("text/html", "application/xhtml+xml")
                    ):
                        raise JobImportError(
                            "That link did not return an HTML job offer page."
                        )

                    content_length = response.headers.get("Content-Length")
                    if (
                        content_length
                        and int(content_length) > MAX_PAGE_BYTES
                    ):
                        raise JobImportError(
                            "The job offer page is too large to import."
                        )

                    chunks = []
                    total_bytes = 0
                    for chunk in response.iter_content(chunk_size=65536):
                        total_bytes += len(chunk)
                        if total_bytes > MAX_PAGE_BYTES:
                            raise JobImportError(
                                "The job offer page is too large to import."
                            )
                        chunks.append(chunk)

                    encoding = response.encoding or "utf-8"
                    html_text = b"".join(chunks).decode(
                        encoding,
                        errors="replace"
                    )
                    return html_text, response.url
                finally:
                    response.close()
    except JobImportError:
        raise
    except (requests.RequestException, UnicodeError, LookupError, ValueError) as error:
        raise JobImportError(
            f"Could not fetch the public job offer page ({error}). "
            "You can still enter the details manually."
        ) from error

    raise JobImportError("Could not fetch the public job offer page.")


def _meta_content(soup, *selectors):
    for selector in selectors:
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def _recruitee_location(soup):
    location_parts = []
    for selector in (
        '[class*="custom-css-style-job-location-city"]',
        '[class*="custom-css-style-job-location-region"]',
        '[class*="custom-css-style-job-location-country"]',
    ):
        for element in soup.select(selector):
            value = element.get_text(" ", strip=True).strip(" ,")
            if value and value not in location_parts:
                location_parts.append(value)

    location = ", ".join(location_parts)
    heading = soup.find("h1")
    if heading:
        header = heading.parent
        for item in header.find_all("li"):
            value = re.sub(r"\s+", " ", item.get_text(" ", strip=True)).strip()
            if value.casefold() == "remote":
                if "Remote" not in location:
                    location = f"Remote, {location}" if location else "Remote"
                break

    return location


def _description_from_page(soup):
    for selector in (
        '[itemprop="description"]',
        '[class*="job-description"]',
        '[class*="description"]',
    ):
        element = soup.select_one(selector)
        if element:
            text = element.get_text("\n", strip=True)
            if len(text) >= 100:
                return text

    for heading in soup.find_all(["h2", "h3"]):
        heading_text = heading.get_text(" ", strip=True).casefold()
        if heading_text not in {"job description", "description"}:
            continue
        container = heading.parent
        text = container.get_text("\n", strip=True)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines and lines[0].casefold() == heading_text:
            lines = lines[1:]
        description = "\n".join(lines)
        if len(description) >= 100:
            return description

    for element in soup.find_all(["article", "main"]):
        text = element.get_text("\n", strip=True)
        if len(text) >= 300:
            return text

    return ""


def _employment_type_from_posting(value):
    if isinstance(value, list):
        value = value[0] if value else ""
    normalized = str(value or "").strip().replace("_", " ").casefold()
    return {
        "full time": "Full-time",
        "part time": "Part-time",
        "contract": "Contract",
        "temporary": "Contract",
        "intern": "Internship",
        "internship": "Internship",
    }.get(normalized)


def fetch_job_offer(url):
    """Fetch public job details from LinkedIn or another job board."""

    url = (url or "").strip()
    if not url:
        raise JobImportError("Paste a job offer link first.")

    linkedin_url = normalize_linkedin_url(url)
    request_url = linkedin_url or url
    page_html, final_url = _fetch_public_page(request_url)

    soup = BeautifulSoup(page_html, "html.parser")
    posting = _job_posting_from_json_ld(soup)

    title = str(posting.get("title") or "").strip()
    organization = posting.get("hiringOrganization", {})
    if isinstance(organization, list):
        organization = organization[0] if organization else {}
    company = (
        str(organization.get("name") or "").strip()
        if isinstance(organization, dict)
        else str(organization or "").strip()
    )
    location = _location_from_posting(posting)
    description = _text_from_html(posting.get("description"))
    date_posted = normalize_date_posted(posting.get("datePosted"))
    employment_type = _employment_type_from_posting(
        posting.get("employmentType")
    )

    h1 = soup.find("h1")
    title = title or (h1.get_text(" ", strip=True) if h1 else "")
    company = company or _meta_content(
        soup,
        'meta[property="og:site_name"]',
        'meta[name="application-name"]',
        'meta[name="author"]',
    )

    if not description:
        description = _description_from_page(soup)

    if not location:
        location = _recruitee_location(soup)

    if not date_posted:
        date_posted = normalize_date_posted(
            _meta_content(
                soup,
                'meta[property="article:published_time"]',
                'meta[name="datePublished"]',
                'meta[name="datePosted"]',
            )
            or (
                soup.select_one('time[itemprop="datePosted"]').get("datetime")
                if soup.select_one('time[itemprop="datePosted"]')
                else ""
            )
        )

    if not title or not company:
        og_title = _meta_content(soup, 'meta[property="og:title"]') or (
            soup.title.get_text(" ", strip=True) if soup.title else ""
        )
        if not title and og_title:
            if company and og_title.casefold().startswith(company.casefold()):
                title = re.sub(
                    rf"^{re.escape(company)}\s*[-|–—:]\s*",
                    "",
                    og_title,
                    flags=re.IGNORECASE
                )
            else:
                title = re.split(r"\s+[|]\s+", og_title)[0].strip()

        for separator in (" | ", " - ", " – ", " — "):
            parts = [part.strip() for part in og_title.split(separator)]
            if len(parts) < 2 or not title or company:
                continue
            title_text = title.casefold()
            if any(title_text in part.casefold() for part in parts[1:]):
                company = parts[0]
            elif title_text in parts[0].casefold():
                company = parts[1]
            if company:
                break

    if not title or not company:
        raise JobImportError(
            "The page did not provide both a job title and company. "
            "You can still enter the details manually."
        )

    return {
        "url": final_url,
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "date_posted": date_posted,
        "employment_type": employment_type,
    }


def fetch_linkedin_job(url):
    """Backward-compatible LinkedIn job importer."""

    if not normalize_linkedin_url(url):
        raise JobImportError(
            "Enter a LinkedIn job URL or use the general job offer importer."
        )
    return fetch_job_offer(url)
