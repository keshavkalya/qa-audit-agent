# QA Audit Agent

A small Python tool that crawls a website with a real browser, records
concrete QA problems as it goes, and produces a readable report.

## What it checks, on every page it visits

- JavaScript console errors and uncaught exceptions
- Failed network requests (any response with a 4xx/5xx status)
- Broken links (`<a href>` targets confirmed dead with a 404/410)
- Broken images (failed to load, or zero natural size)
- Images missing an `alt` attribute (basic accessibility check)
- Pages that take longer than 5 seconds to reach the `load` state
- Missing or empty `<title>` tags
- Visible form fields (`input`/`select`/`textarea`) with no associated label

It also takes a full-page screenshot of every page it visits.

## Avoiding false positives

Automated crawlers are easy to write but hard to make *trustworthy* — a report
full of noise gets ignored. This tool deliberately suppresses common
false positives:

- **Template `<img>` tags with no `src`** are skipped entirely (they aren't
  real images, just empty placeholders a framework will fill in later).
- **`alt=""` is treated as valid**, because it's the correct HTML for a
  decorative image — only a *completely missing* `alt` is flagged.
- **Hidden form fields** are ignored; a label check on an invisible input is
  meaningless.
- **Bot-blocked responses** (403/429/5xx) are not reported as broken links,
  and any HEAD 404 is re-checked with a real GET before being trusted, since
  many sites serve robots misleading statuses.
- **Console warnings** are dropped; only genuine errors are recorded.

## How it works

1. Starts at `--url` and crawls internal links (same domain) breadth-first,
   up to `--max-pages` pages. External domains and logout/delete-style links
   are never navigated into.
2. Uses Playwright (Chromium, headless) to load each page and gather the
   checks above.
3. Sends the raw list of findings to Gemini (`gemini-flash-latest`) and asks it
   to group related issues, assign a severity (Critical/Major/Minor) to
   each, and write a short executive summary.
4. Writes `reports/report.md` (human-readable) and `reports/findings.json`
   (raw structured data). If the Gemini call fails for any reason, the
   report is still written from the raw findings -- the crawl is never lost.

Playwright does the *detection* (it's the browser doing the looking); Gemini
only does the *write-up*. Tuning what counts as a real problem happens in the
crawler's rules, not in the AI.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Set your Gemini API key (get one at https://aistudio.google.com/apikey):

```bash
export GEMINI_API_KEY=your-key-here
```

If the key isn't set, the agent still runs and writes a report -- it just
skips the AI summary and prints a reminder of how to set the key.

## Usage

```bash
python agent.py --url https://example.com --max-pages 10
```

- `--url` (required): the page to start crawling from.
- `--max-pages` (default `10`): maximum number of pages to visit.

## Output

- `reports/report.md` -- executive summary, then issues grouped by severity,
  each with the page URL, what's wrong, and how to reproduce it.
- `reports/findings.json` -- the raw findings (page, issue type, detail).
- `screenshots/` -- one full-page PNG per page visited.

## Being polite to the sites it crawls

- 1 second pause between page visits.
- Only crawls links on the same domain as the start URL.
- Never navigates into logout/delete/signout-style links.
