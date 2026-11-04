#!/usr/bin/env python3
"""Deterministic eligibility and ATS hints; local dedup, no Sheets by default."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

BASE = Path.home() / "Documents/job-agent"
TRACKER = BASE / "tracker.py"
PROFILE = BASE / "profile.json"
NOTIFIER = BASE / "notifier.py"
MAANGO = {"meta", "amazon", "apple", "netflix", "google", "microsoft"}
SUBSIDIARY_DOMAINS = {
    "amazon.jobs": "Amazon", "aws.amazon.com": "Amazon",
    "metacareers.com": "Meta", "facebook.com": "Meta", "instagram.com": "Meta",
    "jobs.apple.com": "Apple", "jobs.netflix.com": "Netflix",
    "careers.google.com": "Google", "youtube.com": "Google",
    "careers.microsoft.com": "Microsoft", "linkedin.com": "Microsoft",
}
ATS = [
    ("greenhouse.io", "Greenhouse"), ("myworkdayjobs.com", "Workday"),
    ("myworkdaysite.com", "Workday"), ("lever.co", "Lever"),
    ("ashbyhq.com", "Ashby"), ("smartrecruiters.com", "SmartRecruiters"),
    ("bamboohr.com", "BambooHR"), ("icims.com", "iCIMS"),
    ("jobvite.com", "Jobvite"), ("taleo.net", "Taleo"),
    ("successfactors.com", "SAP SuccessFactors"),
    ("oraclecloud.com", "Oracle"), ("njoyn.com", "Njoyn"),
]


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    keep = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in {"ref", "source", "trk", "trackingid", "gh_src"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(keep), ""))


def detect_ats(url: str) -> str:
    """Routing hint only, never a claim of supported filling or submission."""
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    detected = next((name for domain, name in ATS if host == domain or host.endswith("." + domain)), None)
    if detected:
        return detected
    # Verified custom Oracle career host, not arbitrary paths/query text.
    if host == "careers.americanexpress.com" and re.search(r"/sites/CX_\d+/job/[^/]+", parts.path):
        return "Oracle"
    return "Unknown"


def unique_jobs(jobs: list[dict]) -> list[dict]:
    """Keep the first candidate for each normalized official URL."""
    seen: set[str] = set()
    unique: list[dict] = []
    for job in jobs:
        key = normalize_url(job.get("url", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def maango_company(company: str, url: str) -> str | None:
    subsidiaries = {"aws": "Amazon", "amazon web services": "Amazon", "instagram": "Meta",
                    "facebook": "Meta", "youtube": "Google", "linkedin": "Microsoft"}
    name = re.sub(r"[,.]", "", company.strip().casefold())
    name = re.sub(r"\s+(?:inc|llc|ltd|corporation)$", "", name)
    if name in subsidiaries:
        return subsidiaries[name]
    words = set(re.findall(r"[a-z0-9]+", company.casefold()))
    hit = MAANGO & words
    if hit and not words.intersection({"credit", "union", "bank"}):
        return sorted(hit)[0].title()
    host = urlsplit(url).netloc.casefold()
    for domain, parent in SUBSIDIARY_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return parent
    return None


def relevant(role: str, profile: dict, extra_text: str = "") -> bool:
    low = role.casefold()
    level_text = " ".join(part.casefold() for part in (role, extra_text) if part)
    preferences = profile.get("preferences", {})
    role_terms = [x.casefold().replace(" intern", "") for x in (preferences.get("target_roles") or [])]
    target = any(term in low for term in role_terms)
    aliases = {
        "software engineer": (r"software development engineer", r"software developer",
                              r"(?:front|back)[ -]?end engineer", r"full[ -]?stack engineer(?:ing)?",
                              r"product engineer(?:ing)?", r"forward deployed engineer",
                              r"\bswe\b"),
        "ai/ml engineer": (r"\bai engineer", r"\bml engineer", r"machine learning engineer",
                           r"artificial intelligence engineer", r"machine learning",
                           r"artificial intelligence", r"\bai\b(?![ -]?\w*\bops\b)"),
        "product management": (r"product manager", r"product management"),
        "business analyst": (r"business analytics",),
        "data analytics": (r"data analyst",),
        "data science": (r"data scientist",),
    }
    if not target:
        target = any(re.search(alias, low) for term in role_terms for alias in aliases.get(term, ()))
    # A posting may name the discipline without the exact "<X> Engineer" noun phrase
    # ("Internship - Software", "Engineering Track"). Accept the discipline token only
    # when the title is not owned by a non-engineering function.
    if not target and any(term in role_terms for term in ("software engineer", "software engineering")):
        non_engineering = re.search(
            r"\b(sales|marketing|recruit\w*|business develop\w*|customer success|"
            r"finance|accounting|legal|communications|design(?:er)?)\b", low)
        if not non_engineering and re.search(r"\b(software|engineer(?:ing)?|developer)\b", low):
            target = True
    level_patterns = {
        "intern": r"\bintern(?:ship)?s?\b",
        "co op": r"\bco[ -]?op\b",
        "new grad": r"\bnew[ -]grad(?:uate)?s?\b",
        "entry level": r"\bentry[ -]level\b",
        "fellow": r"\bfellow(?:ship)?s?\b",
    }
    # Missing keeps legacy defaults; an explicitly unknown level matches none.
    allowed_levels = preferences.get("target_levels", level_patterns) or []
    level = any(
        re.search(level_patterns[key], level_text)
        for item in allowed_levels
        for key in [item.casefold().replace("-", " ")]
        if key in level_patterns
    )
    # "Product Manager" is a target function here, not a seniority signal; strip that exact
    # phrase before scanning so "Senior Product Manager" and "Engineering Manager" still reject.
    seniority_text = re.sub(r"\bproduct (?:manager|management)\b", " ", low)
    senior = bool(re.search(r"\b(senior|staff|principal|lead|manager|director|vp)\b", seniority_text))
    return target and level and not senior


US_COUNTRY_PATTERN = r"(?<![a-z])(?:united states(?: of america)?|u\.?s\.?(?:a\.?)?)(?![a-z])"
US_STATE_CODES = frozenset("""
AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT
NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC
""".split())
US_STATE_NAMES = tuple("""Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|
Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|
Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|
Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|
Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|
Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia""".replace("\n", "").casefold().split("|"))


FOREIGN_LOCATION_PATTERN = (
    r"\b(?:austria|australia|canada|united kingdom|uk|england|scotland|ireland|france|"
    r"germany|netherlands|spain|portugal|italy|switzerland|sweden|norway|denmark|poland|"
    r"india|china|japan|singapore|new zealand|brazil|argentina|israel|south africa)\b"
    r"|,\s*(?:ON|QC|BC|AB|MB|NB|NL|NS|NT|NU|PE|SK|YT)(?=$|[\s,);])"
)


def location_eligibility(location: str | None, profile: dict) -> str:
    """Conservative geographic screen, not a work-authorization determination.

    Bare remote, unrecognized places, and ambiguous CA/Georgia need exact
    employer verification. Never infer geography from an arbitrary substring.
    """
    if not isinstance(location, str) or not location.strip():
        return "needs_verification"
    low = location.strip().casefold()
    preference = (profile.get("preferences", {}).get("location_preference") or "").casefold()
    if preference and not re.search(US_COUNTRY_PATTERN, preference):
        return "eligible"
    us = re.search(US_COUNTRY_PATTERN, low)
    foreign = re.search(FOREIGN_LOCATION_PATTERN, location, re.I)
    if re.search(r"\b(?:except|excluding|outside|not in)\s+(?:the\s+)?" + US_COUNTRY_PATTERN, low):
        return "ineligible"
    if foreign and re.search(r"(?:" + FOREIGN_LOCATION_PATTERN + r")[\s-]+only\b", location, re.I):
        return "ineligible"
    if us:
        if re.search(US_COUNTRY_PATTERN + r"(?:[ -]based)?\s+(?:time\s*zones?|hours|team|company)\b", low):
            return "needs_verification"
        return "eligible"
    if foreign:
        return "ineligible"
    if re.search(r"\bremote\b", low) and re.search(r"\b(worldwide|anywhere in the world|global)\b", low):
        return "eligible"
    # State codes require a city/region delimiter; CA alone also means Canada.
    if re.search(r"[,(/;]\s*(?:" + "|".join(sorted(US_STATE_CODES)) + r")(?=$|[\s),;/])", location):
        return "eligible"
    if low != "georgia" and any(re.search(r"\b" + re.escape(state) + r"\b", low) for state in US_STATE_NAMES):
        return "eligible"
    return "needs_verification"


def location_allowed(location: str | None, profile: dict) -> bool:
    return location_eligibility(location, profile) == "eligible"


def rejection_reasons(job: dict, profile: dict) -> list[str]:
    reasons: list[str] = []
    if not relevant(job["role"], profile, extra_text=str(job.get("season", ""))):
        reasons.append("role:not_target_level")
    if location_eligibility(job.get("location"), profile) == "ineligible":
        reasons.append("location:not_us_or_remote")
    timeline = " ".join(
        str(value).strip() for value in (job.get("season"), job.get("role")) if str(value).strip()
    ).casefold()
    configured_timelines = profile.get("preferences", {}).get("target_timelines", [])
    if configured_timelines is None:
        # Clearing a preference must not silently turn off its constraint.
        reasons.append("timeline:unknown")
    target_timelines = [item.casefold() for item in (configured_timelines or [])]
    if target_timelines and any(season in timeline for season in ("winter", "spring", "summer", "fall")):
        if not any(target in timeline for target in target_timelines):
            reasons.append("timeline:not_target")
    require_sponsorship = profile.get("screening_defaults", {}).get("require_sponsorship")
    if require_sponsorship is False and job.get("requires_sponsorship") is True:
        reasons.append("eligibility:sponsorship_required")
    return reasons


def tracker_duplicate(company: str, role: str, url: str) -> bool:
    proc = subprocess.run([sys.executable, str(TRACKER), "check", "--company", company,
                           "--role", role, "--url", normalize_url(url)], capture_output=True, text=True)
    if proc.returncode not in (0, 10):
        raise RuntimeError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout)["duplicate"]


def classify(job: dict, profile: dict, *, known_urls=(), duplicate_checker=None, use_tracker: bool = False) -> dict:
    """Classify discovery only; injected dedup checks must concern this exact URL.

    No local match is not proof of no prior application. Submission-time portal
    reconciliation remains required. Sheets is read only with explicit opt-in.
    """
    company, role = job["company"].strip(), job["role"].strip()
    url = normalize_url(job["url"])
    parent = maango_company(company, url)
    reasons = rejection_reasons({**job, "company": company, "role": role, "url": url}, profile)
    verification = (["location:needs_verification"]
                    if location_eligibility(job.get("location"), profile) == "needs_verification" else [])
    status = "ineligible" if reasons else "needs_verification" if verification else "eligible"
    return {**job, "company": company, "role": role, "url": url,
            "ats_platform": detect_ats(url), "relevant": status == "eligible",
            "eligibility_status": status, "verification_reasons": verification,
            "rejection_reasons": reasons,
            "duplicate": (url in {normalize_url(known) for known in known_urls}
                          or (duplicate_checker is not None and duplicate_checker(company, role, url))
                          or (use_tracker is True and tracker_duplicate(company, role, url))),
            "manual_only": bool(parent), "maango_parent": parent,
            "priority_monitor": parent == "Amazon" and bool(re.search(
                r"\bsummer[\s,-]+2027\b|\b2027[\s,-]+summer\b",
                role + " " + str(job.get("season", "")), re.I)),
            }


def known_urls_from_queue(path: str | Path) -> set[str]:
    """Read exact durable queue identities without creating/migrating the DB.

    Every queue state counts as already discovered, including uncertain/failed
    attempts. Discovery must not replay them. Corrupt state is an error.
    """
    path = Path(path)
    if not path.exists():
        return set()
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        return {normalize_url(row[0]) for row in connection.execute(
            "SELECT normalized_url FROM application_queue"
        )}


def _observation_only(job: dict) -> bool:
    """Explicit non-live or malformed provenance cannot become a queue hint."""
    return (job.get("live_verified", True) is not True
            or job.get("source_live_verified", True) is not True
            or job.get("pilot_only", False) is not False)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("input", nargs="?", help="JSON array file; omit for stdin")
    p.add_argument("--notify-maango", action="store_true")
    p.add_argument("--output", default=str(BASE / "scan-results.json"))
    p.add_argument("--profile", default=str(PROFILE))
    p.add_argument("--queue-db", default=str(BASE / "runtime/app_queue.sqlite3"))
    args = p.parse_args(argv)
    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    text = Path(args.input).read_text(encoding="utf-8") if args.input else sys.stdin.read()
    raw_jobs = json.loads(text)
    observation_urls = {normalize_url(job.get("url", "")) for job in raw_jobs if _observation_only(job)}
    jobs = unique_jobs(raw_jobs)
    known_urls = known_urls_from_queue(args.queue_db)
    results = [classify(job, profile, known_urls=known_urls) for job in jobs]
    observations = [x for x in results if x["url"] in observation_urls]
    new = [x for x in results if x["relevant"] and not x["duplicate"] and x["url"] not in observation_urls]
    manual = [x for x in new if x["manual_only"]]
    queue = [x for x in new if not x["manual_only"]]
    if args.notify_maango:
        for job in manual:
            subprocess.run([sys.executable, str(NOTIFIER), "maango", "--company", job["company"],
                            "--role", job["role"], "--url", job["url"],
                            "--detail", "Found by the job scanner. Kevin should review and apply manually."], check=True)
    payload = {"scanned": len(results), "new": len(new), "manual_only": manual,
               "auto_apply_queue": queue, "all_results": results,
               "observation_only_results": observations}
    Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
