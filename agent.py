#!/usr/bin/env python3
"""
QA Audit Agent
==============
Crawls a website with a real browser (Playwright + Chromium), records
concrete problems it finds along the way (JS errors, broken links/images,
slow pages, accessibility gaps, etc.), then asks Gemini to turn the raw
findings into a readable, severity-ranked report.

Usage:
    python agent.py --url https://example.com --max-pages 10

Output:
    reports/report.md      human-readable report (AI summary + findings)
    reports/findings.json  raw structured findings (always written)
    screenshots/*.png      one full-page screenshot per page visited
"""

import argparse
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# --------------------------------------------------------------------------
# Config constants
# --------------------------------------------------------------------------

GEMINI_MODEL = "gemini-flash-latest"  # gemini-2.5-flash 404s as "no longer available
                                       # to new users" on some API keys; this alias
                                       # always points at Google's current flash model.
SLOW_LOAD_SECONDS = 5.0          # pages that take longer than this are flagged
POLITE_DELAY_SECONDS = 1.0       # pause between pages so we don't hammer the site
NAV_TIMEOUT_MS = 30_000          # give a page 30s to load before giving up
LINK_CHECK_TIMEOUT_MS = 10_000   # give a link-status check 10s before giving up
SKIP_LINK_PATTERNS = re.compile(r"logout|log-out|signout|sign-out|delete", re.I)

SCREENSHOTS_DIR = Path("screenshots")
REPORTS_DIR = Path("reports")

# Playwright's default headless UA contains "HeadlessChrome", which some
# sites (LinkedIn, Facebook, etc.) block or answer differently than they
# would a normal browser. Presenting as a regular desktop Chrome avoids
# those false-positive "broken link" results.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------
# Data model: every problem we find becomes one Finding
# --------------------------------------------------------------------------

@dataclass
class Finding:
    page: str        # the URL of the page the problem was observed on
    issue_type: str  # short machine-readable category, e.g. "broken_link"
    detail: str      # human-readable description of the specific problem


# --------------------------------------------------------------------------
# Small URL helpers
# --------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Strip the #fragment so we don't treat 'page#a' and 'page#b' as different pages."""
    return url.split("#")[0].rstrip("/")


def same_domain(url: str, base_domain: str) -> bool:
    """True if url belongs to the same site we started crawling (ignoring www.)."""
    netloc = urlparse(url).netloc.lower().removeprefix("www.")
    return netloc == base_domain


def is_crawlable(url: str) -> bool:
    """
    Decide whether we should ever navigate to this URL.
    We refuse to click into logout/delete-style links (rule: be polite,
    don't trigger destructive actions on someone else's site) and we
    only handle http(s) links (skip mailto:, tel:, javascript:, etc).
    """
    if not url.startswith(("http://", "https://")):
        return False
    if SKIP_LINK_PATTERNS.search(url):
        return False
    return True


# --------------------------------------------------------------------------
# Per-page auditing
# --------------------------------------------------------------------------

def audit_page(context, url: str, base_domain: str, link_status_cache: dict):
    """
    Visit a single URL, collect every QA finding on it, take a screenshot,
    and return (findings, links_found_on_page).

    A fresh Page is used per visit so that event listeners (console,
    response) are cleanly scoped to just this one page load.
    """
    findings = []
    page = context.new_page()

    # --- Listener: JS console errors & warnings -------------------------
    console_messages = []

    def on_console(msg):
        # Only real errors. Most sites emit harmless console warnings that
        # would otherwise flood the report with non-actionable noise.
        if msg.type == "error":
            console_messages.append((msg.type, msg.text))

    page.on("console", on_console)
    # Uncaught JS exceptions don't go through "console" - catch those too.
    page.on("pageerror", lambda exc: console_messages.append(("error", str(exc))))

    # --- Listener: failed network requests (4xx/5xx) --------------------
    failed_requests = []

    def on_response(response):
        if response.status >= 400:
            failed_requests.append((response.url, response.status))

    page.on("response", on_response)

    # --- Navigate and time how long it takes to reach "load" ------------
    start = time.perf_counter()
    try:
        page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        findings.append(Finding(url, "slow_load", f"Page did not reach load state within {NAV_TIMEOUT_MS/1000:.0f}s"))
    load_time = time.perf_counter() - start

    if load_time > SLOW_LOAD_SECONDS:
        findings.append(Finding(url, "slow_load", f"Page took {load_time:.1f}s to reach load state (>{SLOW_LOAD_SECONDS:.0f}s threshold)"))

    # --- Title check ------------------------------------------------------
    title = page.title().strip()
    if not title:
        findings.append(Finding(url, "missing_title", "Page has no <title>, or the title is empty"))

    # --- Turn collected console/network events into findings ------------
    for msg_type, text in console_messages:
        findings.append(Finding(url, "console_error", f"Console {msg_type}: {text}"))

    for req_url, status in failed_requests:
        findings.append(Finding(url, "network_error", f"Request to {req_url} failed with status {status}"))

    # --- Images: broken/zero-size and missing alt text -------------------
    images = page.eval_on_selector_all(
        "img",
        """(imgs) => imgs.map(img => ({
                src: img.currentSrc || img.src,
                alt: img.getAttribute('alt'),
                naturalWidth: img.naturalWidth,
                naturalHeight: img.naturalHeight,
                complete: img.complete
           }))""",
    )
    for img in images:
        src = img["src"]
        # Skip placeholder/template <img> tags that have no src at all --
        # there's nothing real to load or judge, so flagging them is noise.
        if not src:
            continue
        if img["complete"]:
            # The browser actually attempted this one -- trust its verdict.
            if img["naturalWidth"] == 0 and img["naturalHeight"] == 0:
                findings.append(Finding(url, "broken_image", f"Image failed to load or has zero size: {src}"))
        else:
            # Lazy-loaded images the browser never got around to fetching
            # (e.g. still off-screen, or sized 0 by CSS until JS resizes
            # them post-load, which can stop native lazy-load from ever
            # triggering) can't be judged by naturalWidth. Check the URL
            # directly instead, the same way we check links.
            if src not in link_status_cache:
                link_status_cache[src] = check_link_status(context, src)
            status = link_status_cache[src]
            if status in (404, 410):
                findings.append(Finding(url, "broken_image", f"Image failed to load: {src} (status {status})"))
        # alt="" is valid HTML -- it marks a decorative image. Only a
        # completely missing alt attribute is an accessibility problem.
        if img["alt"] is None:
            findings.append(Finding(url, "missing_alt", f"Image missing alt text: {src}"))

    # --- Forms: inputs without an associated label ------------------------
    unlabeled = page.eval_on_selector_all(
        "input, select, textarea",
        """(fields) => fields
            .filter(f => !['hidden', 'submit', 'button', 'image'].includes((f.type || '').toLowerCase()))
            .filter(f => f.offsetParent !== null)
            .filter(f => {
                const hasAriaLabel = f.hasAttribute('aria-label') && f.getAttribute('aria-label').trim() !== '';
                const hasAriaLabelledBy = f.hasAttribute('aria-labelledby');
                const wrappedInLabel = f.closest('label') !== null;
                const hasForLabel = f.id && document.querySelector(`label[for="${f.id}"]`) !== null;
                return !(hasAriaLabel || hasAriaLabelledBy || wrappedInLabel || hasForLabel);
            })
            .map(f => f.outerHTML.slice(0, 120))""",
    )
    for snippet in unlabeled:
        findings.append(Finding(url, "unlabeled_input", f"Form field has no associated label: {snippet}"))

    # --- Links: gather for crawling, and check for broken ones -----------
    hrefs = page.eval_on_selector_all("a[href]", "(as) => as.map(a => a.getAttribute('href'))")
    links_found = []
    checked_on_this_page = set()

    for href in hrefs:
        if not href or href.startswith("#") or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = normalize_url(urljoin(url, href))
        if not is_crawlable(absolute):
            continue

        # Queue same-domain links for the crawler to visit later.
        if same_domain(absolute, base_domain):
            links_found.append(absolute)

        # Check every unique link (internal or external) for a dead status,
        # but only once per link across the whole crawl (cache the result).
        if absolute in checked_on_this_page:
            continue
        checked_on_this_page.add(absolute)

        if absolute not in link_status_cache:
            link_status_cache[absolute] = check_link_status(context, absolute)
        status = link_status_cache[absolute]
        # Only "hard dead" statuses are real broken links. 403/429/5xx are
        # usually bot-blocking or transient server hiccups, not genuine bugs.
        if status in (404, 410):
            findings.append(Finding(url, "broken_link", f"Link to {absolute} returned status {status}"))

    # --- Screenshot --------------------------------------------------------
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", url).strip("_")[:150] or "page"
    page.screenshot(path=str(SCREENSHOTS_DIR / f"{safe_name}.png"), full_page=True)

    page.close()
    return findings, links_found


def check_link_status(context, url: str):
    """
    Ask "is this link alive?" using a lightweight HEAD request (falling back
    to GET, since some servers reject HEAD). Returns the HTTP status code,
    or None if the request errored out entirely (DNS failure, timeout, etc.)
    -- we don't report those as broken links since it's not a clean 4xx/5xx.
    """
    try:
        response = context.request.head(url, timeout=LINK_CHECK_TIMEOUT_MS)
        status = response.status
        # Some servers reject HEAD (or bot-block it) with 400/403/404/405/429
        # even though a real GET works fine. Confirm with a GET before we
        # trust the failure, so we don't report false broken links/images.
        if status in (400, 403, 404, 405, 429):
            response = context.request.get(url, timeout=LINK_CHECK_TIMEOUT_MS)
            status = response.status
        return status
    except Exception:
        return None


# --------------------------------------------------------------------------
# Crawl orchestration
# --------------------------------------------------------------------------

def crawl(start_url: str, max_pages: int) -> list[Finding]:
    """Breadth-first crawl of the site starting at start_url, up to max_pages."""
    base_domain = urlparse(start_url).netloc.lower().removeprefix("www.")
    start_url = normalize_url(start_url)

    visited = set()
    queue = [start_url]
    all_findings = []
    link_status_cache = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=BROWSER_USER_AGENT)

        while queue and len(visited) < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            print(f"  Visiting ({len(visited)}/{max_pages}): {url}")
            try:
                findings, links = audit_page(context, url, base_domain, link_status_cache)
            except Exception as exc:
                # A single bad page shouldn't kill the whole crawl.
                findings, links = [Finding(url, "crawl_error", f"Failed to audit page: {exc}")], []
            all_findings.extend(findings)

            for link in links:
                if link not in visited and link not in queue:
                    queue.append(link)

            # Be polite: pause before hitting the next page.
            if queue and len(visited) < max_pages:
                time.sleep(POLITE_DELAY_SECONDS)

        browser.close()

    return all_findings


# --------------------------------------------------------------------------
# Gemini: turn raw findings into a grouped, severity-ranked summary
# --------------------------------------------------------------------------

def summarize_with_gemini(findings: list[Finding]):
    """
    Send the raw findings to Gemini and ask it to group them, assign a
    severity (Critical / Major / Minor) to each, and write an executive
    summary. Returns a dict on success, or None on any failure -- callers
    must fall back to the raw findings so a Gemini outage never loses data.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(
            "\n[!] GEMINI_API_KEY is not set, so the report will be written "
            "without an AI summary.\n"
            "    Get a key at https://aistudio.google.com/apikey and set it, e.g.:\n"
            "    export GEMINI_API_KEY=your-key-here\n"
        )
        return None

    try:
        from google import genai

        client = genai.Client(api_key=api_key)

        prompt = f"""You are a QA analyst. Below is a raw JSON list of website
issues found by an automated crawler. Each item has "page" (the URL),
"issue_type", and "detail".

Group related issues, assign each one a severity of exactly "Critical",
"Major", or "Minor", and write a 2-4 sentence executive summary of the
overall site health.

Respond with ONLY valid JSON in this exact shape, no other text:
{{
  "executive_summary": "string",
  "issues": [
    {{
      "severity": "Critical" | "Major" | "Minor",
      "page": "string",
      "issue_type": "string",
      "description": "string",
      "how_to_reproduce": "string"
    }}
  ]
}}

Raw findings:
{json.dumps([asdict(f) for f in findings], indent=2)}
"""

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )

        text = response.text.strip()
        # Defensive: strip markdown code fences if the model adds them anyway.
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        return json.loads(text)

    except Exception as exc:
        print(f"\n[!] Gemini summarization failed ({exc}); writing report from raw findings instead.\n")
        return None


# --------------------------------------------------------------------------
# Report writing
# --------------------------------------------------------------------------

def write_findings_json(findings: list[Finding]):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORTS_DIR / "findings.json", "w") as f:
        json.dump([asdict(f) for f in findings], f, indent=2)


def write_report_md(findings: list[Finding], ai_result: dict | None, start_url: str, max_pages: int):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append(f"# QA Audit Report\n")
    lines.append(f"- **Site audited:** {start_url}")
    lines.append(f"- **Pages crawled (max):** {max_pages}")
    lines.append(f"- **Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **Total raw findings:** {len(findings)}\n")

    if ai_result:
        lines.append("## Executive Summary\n")
        lines.append(ai_result.get("executive_summary", "").strip() + "\n")

        issues = ai_result.get("issues", [])
        for severity in ("Critical", "Major", "Minor"):
            group = [i for i in issues if i.get("severity") == severity]
            if not group:
                continue
            lines.append(f"## {severity} ({len(group)})\n")
            for issue in group:
                lines.append(f"### {issue.get('issue_type', 'issue')} — {issue.get('page', '')}")
                lines.append(f"- **What's wrong:** {issue.get('description', '')}")
                lines.append(f"- **How to reproduce:** {issue.get('how_to_reproduce', '')}\n")
    else:
        # Fallback path: no AI summary available, list raw findings grouped by type.
        lines.append("## Executive Summary\n")
        lines.append("_AI summary unavailable -- showing raw crawl findings below, grouped by issue type._\n")

        by_type: dict[str, list[Finding]] = {}
        for finding in findings:
            by_type.setdefault(finding.issue_type, []).append(finding)

        for issue_type, group in sorted(by_type.items()):
            lines.append(f"## {issue_type} ({len(group)})\n")
            for finding in group:
                lines.append(f"- **Page:** {finding.page}")
                lines.append(f"  **Detail:** {finding.detail}\n")

    with open(REPORTS_DIR / "report.md", "w") as f:
        f.write("\n".join(lines))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="QA Audit Agent: crawl a site and report real problems.")
    parser.add_argument("--url", required=True, help="Starting page to crawl")
    parser.add_argument("--max-pages", type=int, default=10, help="Maximum number of pages to visit (default: 10)")
    args = parser.parse_args()

    print(f"Starting crawl of {args.url} (max {args.max_pages} pages)...")
    findings = crawl(args.url, args.max_pages)
    print(f"\nCrawl complete: {len(findings)} raw findings collected.")

    print("Sending findings to Gemini for grouping and severity scoring...")
    ai_result = summarize_with_gemini(findings)

    write_findings_json(findings)
    write_report_md(findings, ai_result, args.url, args.max_pages)

    print(f"\nDone. See {REPORTS_DIR / 'report.md'} and {REPORTS_DIR / 'findings.json'}.")


if __name__ == "__main__":
    main()
