# QASpider

A crawler that inventories a website. Give it one URL, and it walks every page inside that URL's scope, recording the presence or absence of **66 element categories** per page in a `.json` report.

This is not an accessibility auditor. It does not compute scores, WCAG conformance, or PASS/FAIL. It inventories what is there.

---

## What it does

1. Takes a base URL and uses it as a **scope floor**, not merely an origin.
2. Discovers URLs from the rendered DOM, `robots.txt`, and sitemaps.
3. Drains the queue with no implicit caps.
4. Writes a `.json` report with one record per page plus a statistics block.

### How scope is defined

Scope is the full base URL, compared by path segments. This is the part that matters:

| Base URL | Discovered URL | In scope? |
| --- | --- | --- |
| `https://site.com/demo/mars/` | `https://site.com/demo/mars/mars2.html?a=hotels` | Yes |
| `https://site.com/demo/mars/` | `https://site.com/demo/mars2.html` | No (a sibling, not a child) |
| `https://site.com/demo/mars/` | `https://site.com/other/route` | No |
| `https://site.com/demo/mars/` | `https://other-site.com/x` | No |

Comparison is segment-based, so `/demo/mars` never captures `/demo/marsx`. **Query strings are preserved**: `?a=hotels`, `?a=cart`, and `?a=mars_map` are distinct pages and are inventoried separately.

> The trailing slash is significant. Starting at `/demo/mars/` makes a relative link `mars2.html?a=x` resolve to `/demo/mars/mars2.html?a=x`. Starting at `/demo/mars` (no slash) resolves it to `/demo/mars2.html`, which falls outside the scope. That is standard relative-URL resolution, not a special case.

---

## Requirements

- **Python 3.10 or newer** (required by `playwright>=1.63`; verified on 3.11 and 3.12).
- Windows, Linux, or macOS.

## Installation

```bash
git clone https://github.com/ArgTincho89/QASpider-script.git
cd QASpider-script
pip install -r requirements.txt
playwright install chromium
```

`playwright install chromium` downloads the browser. It is required on first run.

---

## Usage

### The basic command

```bash
python QASpider-script.py "https://www.example.com/" --output "inventory.json"
```

That is the whole interface: **one URL and one output file**. The report lands in `inventory.json`.

On Windows with the Python launcher:

```powershell
py -3.12 .\QASpider-script.py "https://www.example.com/" --output ".\inventory.json"
```

### A real run (bounded, good for a first try)

```bash
python QASpider-script.py "https://www.helbreathargentina.com/" --output "report.json" --max-pages 20
```

### Options

| Option | Default | Purpose |
| --- | --- | --- |
| `--output` | `inventory.json` | Path of the `.json` file to write. |
| `--max-pages` | no cap | Emergency page cap. |
| `--max-depth` | no cap | Emergency link-depth cap. |
| `--timeout` | `30000` | Navigation timeout in milliseconds. |

Without `--max-pages` and `--max-depth` the crawler **does not stop on its own**: it drains the queue. The caps exist for sites that generate an unwieldy URL space and where you want to bound the run.

---

## The `.json` report

### Structure

```json
{
  "startUrl": "https://www.example.com/",
  "pages": [ ... ],
  "stats": { ... }
}
```

### An inventoried page (2xx response)

```json
{
  "url": "https://www.example.com/",
  "elements": {
    "links": true,
    "internalLinks": true,
    "externalLinks": true,
    "navigation": false,
    "headings": true,
    "h1": true,
    "forms": false
  }
}
```

`elements` always contains **exactly the 66 keys**, always present, always `true` or `false`. There are no scores and no severity levels: this is a presence inventory.

### A page that could not be inventoried

When a page cannot be inventoried, nothing is fabricated. It is flagged for manual review:

```json
{
  "url": "https://www.example.com/broken",
  "statusCode": 404,
  "action": "agent-check",
  "error": "HTTP 404"
}
```

| Field | Meaning |
| --- | --- |
| `statusCode` | Present only when an HTTP response was received. |
| `action` | Always `agent-check`: needs a manual check or an authenticated session. |
| `error` | A fixed, sanitized reason. Never contains internal data. |

The set of possible reasons is closed and readable: `HTTP <code>`, `off-origin redirect was blocked.`, `Redirect outside the crawl path was blocked.`, `Redirect loop was blocked.`, `Document exceeded the size limit.`, `Browser closed unexpectedly.`, `Request timed out.`, `Network request failed.`

### Statistics

```json
{
  "discovered": 90,
  "processed": 90,
  "failed": 6,
  "maxPages": null,
  "maxDepth": null,
  "pending": 0,
  "depthSuppressed": 0,
  "complete": false,
  "truncated": false,
  "issues": [],
  "browserLosses": 0,
  "browserRecoveries": 0,
  "maxBrowserRecoveries": 3,
  "stopReason": null,
  "teardownFailures": 0
}
```

| Key | Meaning |
| --- | --- |
| `discovered` | Unique URLs found inside the scope. |
| `processed` | URLs the crawler attempted to inventory. |
| `failed` | Pages left as `agent-check`. |
| `pending` | URLs still queued at the end. `0` means the queue was drained. |
| `complete` | `true` only when nothing is pending and nothing failed. |
| `truncated` | `true` if the run was cut short by a cap or by exhausting the recovery budget. |
| `stopReason` | Why the run stopped, if it did. `null` on a normal finish. |
| `browserLosses` / `browserRecoveries` | How many times the browser died and how many were recovered. |
| `teardownFailures` | Browser or context closes that failed. They do not affect the result. |

**`complete: false` does not always mean something broke.** If any page returned 404, `complete` is `false` because there are `agent-check` records awaiting manual review. To know whether exploration actually finished, look at `pending` and `truncated`.

---

## The 66 categories

**Links and navigation:** `links`, `internalLinks`, `externalLinks`, `navigation`

**Images and media:** `media`, `images`, `picture`, `svg`, `canvas`, `video`, `audio`, `mediaSources`, `track`, `mediaTracks`

**Embeds:** `iframes`, `objects`, `embeds`, `embeddedContent`

**Headings:** `headings`, `h1`, `h2`, `h3`, `h4`, `h5`, `h6`

**Forms:** `forms`, `formControls`, `inputs`, `textareas`, `selects`, `buttons`, `labels`, `fieldsets`

**Tables:** `tables`, `tableHeaders`, `tableCaption`

**Lists:** `lists`, `orderedLists`, `unorderedLists`, `descriptionLists`, `listItems`

**Landmarks and structure:** `landmarks`, `header`, `nav`, `main`, `footer`, `aside`, `section`, `article`

**ARIA and interactivity:** `aria`, `roles`, `interactiveElements`, `tabindex`, `dialogs`, `details`, `summary`

**Content:** `paragraphs`, `time`, `abbr`, `mark`

**Other:** `languageAttributes`, `contentEditable`, `customElements`, `shadowDom`

---

## Behaviour worth knowing

### It only discovers, it never interacts

The crawler is **passive**. It extracts `a[href]`, `area[href]`, and `[role="link"][href]`, and nothing else.

- It **does not click** any control.
- It **does not submit** forms.
- It **does not type** anything.
- It **does not** attempt to log in or bypass authentication.

This is deliberate. A crawler that clicks can trigger real actions on a production site. If you need genuine auditing, run this in a controlled environment.

That is also why pages behind a login, a VPN, or an authenticated session come back as `agent-check`: the crawler cannot get through them on its own. Manage the session from your agent before running it.

### It is not an accessibility auditor

It reports element presence, not conformance. If you need a WCAG evaluation, a contrast report, or a list of violations, treat this report as the **input**: it tells you where structure exists and where it does not.

### Safety and stability

- **TLS is never disabled.** The crawler sets `NODE_USE_SYSTEM_CA=1` only while Playwright is running, so Chrome trusts the operating system certificate store (useful behind a corporate proxy or with internal certificates), and restores the previous value afterwards.
- **Errors are sanitized.** Call logs, cookies, headers, and tokens are never stored. Report reasons are fixed strings.
- **Media subresources are aborted** (video and audio). They are not needed to inventory the DOM and they are the usual cause of memory exhaustion on embed-heavy pages. Images, scripts, styles, and fonts still load, so the DOM renders normally.
- **Documents larger than 10 MB are not buffered.** They are marked `agent-check` instead of risking the process.
- **If the browser dies, the crawler recovers.** It relaunches up to 3 times and resumes the queue where it stopped. Already-inventoried work is never lost: the report is always written, even when a run ends badly.

### Origin and ports

Only the same origin is followed (same scheme, host, and effective port). Redirects to another origin are blocked and recorded.

---

## Project layout

```
QASpider-script/
├── QASpider-script.py    # entrypoint
├── qaspider/
│   ├── cli.py            # arguments and JSON output
│   ├── crawl.py          # engine: queue, scope, redirects, recovery
│   ├── inventory.py      # the 66 categories
│   └── urls.py           # normalization and scope comparison
├── tests/
│   └── test_crawler.py   # 36 tests
├── requirements.txt
└── .gitignore
```

## Tests

```bash
python -m unittest discover -s tests
```

All 36 tests run against a local server and never touch a public site.

## Use as a library

```python
from qaspider import crawl_site

result = crawl_site("https://www.example.com/", max_pages=50)
for page in result["pages"]:
    if "elements" in page:
        print(page["url"], page["elements"]["h1"])
```

## License

No license declared. Add one before redistributing.
