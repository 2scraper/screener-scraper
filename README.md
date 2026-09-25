# screener-scraper

[![release](https://img.shields.io/github/v/release/2scraper/screener-scraper)](https://github.com/2scraper/screener-scraper/releases)
[![tests](https://github.com/2scraper/screener-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/screener-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/screener-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/screener-scraper/actions/workflows/canary.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![account](https://img.shields.io/badge/runs%20without%20an%20account-yes-brightgreen)

A scraper for **[screener.in](https://www.screener.in)** result tables — any
stock screen (`/screens/{id}/{slug}/`) and any sector or industry listing
(`/market/IN08/IN0801/IN080101/`) — to JSON and CSV. Playwright is the primary
engine; Selenium, Puppeteer (pyppeteer) and the 2Captcha Scraper API do the
same job with the same output. Part of the
[2scraper](https://github.com/2scraper) family of single-site scrapers.

No screen or sector is hardcoded: what a run covers is the URL you give it.

## Quick start

```bash
git clone https://github.com/2scraper/screener-scraper && cd screener-scraper
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt -r requirements-playwright.txt
./venv/bin/playwright install chromium

./venv/bin/python playwright_scraper.py \
  --url "https://www.screener.in/market/IN08/IN0801/IN080101/" \
  --pages 50 --out it_software
```

That run asks for 50 pages; the sector has 7, so it fetches 7 and stops —
the scraper reads the site's own "Showing page 1 of 7" and never plans past
the last page. Measured 2026-09-25: **164 rows against the site's own "164
results found", ranks 1–164 with no gap, `status: complete`,
`coverage: exhaustive`, exit 0.**

## What the paid products buy you — and when you need none of them

**From an ordinary connection you need no key, no proxy and no account.** On
2026-09-25 plain `curl` from a residential connection — no browser, no
JavaScript, no cookie — was served a sector page and its page 2, three
screens of very different size, and page 220 of the 5,477-company "All
Stocks" screen, in full. Fifteen back-to-back requests with no delay all
answered HTTP 200. All three browser engines, run within a minute of each
other, returned the same 75 companies with identical quarterly figures on the
same three pages — only prices moved, with the market.

The one gate this repo has evidence of is a **registration wall**: captures
taken on 2026-08-23/24 through the Scraping Browser API and the Scraper API
landed on screener.in's `/register/` page for `/screens/` URLs (and not for
`/market/` ones). The scraper recognises that page, reports it as blocked
(exit 3) with advice, and never parses it as an empty screen. It is not a
captcha: no widget is on it for any solver.

What each 2Captcha product (one key, four separately billed products) buys on
this site:

| Product | What it buys here |
|---|---|
| **Proxies** ([2captcha.com/proxy](https://2captcha.com/proxy), also sold as 2prx.com) | Volume from many addresses, and an Indian exit. `--proxy`, `--proxy-file`, `--proxy-rotate per-page`, and per-worker exits under `--concurrency`. |
| **Scraping Browser API** | No browser infrastructure of your own, a chosen exit country, and a persistent profile. `--cdp-endpoint`. Given the captures above, try a plain connection first for `/screens/`. |
| **Fingerprints** | A complete device identity for the launched browser (`--fingerprint`, Playwright engine). Not needed for anything measured here, and not run live in this version. |
| **Captcha solving** | Nothing on this site as measured — no captcha is configured on any page captured. It is wired in and bounded to one solve per page anyway, because a site can switch one on between deploys. |

## What it extracts

One row per company, in the table's own order.

| Column | What it is |
|---|---|
| `source`, `scraped_at` | `screener.in`, UTC timestamp |
| `url` | The company's own screener.in page |
| `sku` | The site's own company id, from the row's `data-row-company-id` |
| `title` | The company name, as the table links it |
| `price`, `currency` | CMP, and `INR` read from the price column's own unit ("Rs.") — never a default |
| `category` | `--category`, or the listing's own heading ("IT - Software Companies") |
| `page`, `position` | The site page the row came from, and its position on it |
| `rank` | The site's own S.No. — global across pages (page 2 starts at 26) |
| `ticker` | What the company URL carries: letters (`TCS`) or six digits (`532015`) |
| `consolidated` | Whether the row links the consolidated or the standalone figures |
| `market_cap_cr`, `pe`, `dividend_yield_pct`, `net_profit_qtr_cr`, `qtr_profit_var_pct`, `sales_qtr_cr`, `qtr_sales_var_pct`, `roce_pct` | The columns every page kind showed an anonymous visitor on 2026-09-25 |
| `extra_metrics` | Every other column the page showed, keyed by the site's own name and unit, e.g. `{"Return on invested capital (%)": 88.05}`. JSON in the CSV cell. |

A column is identified by its header's own metric name **and unit**, never by
its position — a screen that adds a column moves every column after it. A
header in a different unit goes to `extra_metrics` under its own name rather
than into a typed column that promises a unit it does not have.

See [`sample_output.json`](sample_output.json) — five rows cut from a real
run.

**Run metadata.** Every run that writes output also writes `<out>.meta.json`:
`status` (complete / partial), `stop_reason`, `pages_failed` by number, the
site's own `total_results` and `pages_available`, `coverage` (`exhaustive`
when the run reached the end of the listing, `window` when it stopped at
`--pages`), and `rank_gaps` — site ranks missing from the merged rows, which
is proof of a lost page rather than a guess.

**Exit codes**, shared by every engine: `0` ok · `1` crash · `2` bad usage ·
`3` blocked · `4` the site says the listing is empty · `5` the content was
never obtained · `6` partial.

## Engines

| Script | Engine | Measured 2026-09-25 |
|---|---|---|
| `playwright_scraper.py` | **Playwright** — primary | 3 pages / 75 rows; 7 of 7 pages / 164 rows; `--concurrency 2` over a 3-page screen, 64 of 64 |
| `puppeteer_scraper.py` | pyppeteer | 3 pages / 75 rows — same companies and quarterly figures as Playwright |
| `selenium_scraper.py` | Selenium + Chrome | 3 pages / 75 rows — same companies and quarterly figures as Playwright |
| `scraper_api_client.py` | 2Captcha Scraper API, no local browser | **not run live in this version** — no key was available; exercised offline against real captures |

Install **one** engine per virtualenv: playwright and pyppeteer pin
incompatible `pyee` versions, and pyppeteer and selenium collide on `urllib3`.

```bash
pip install -r requirements.txt -r requirements-playwright.txt   # or -puppeteer / -selenium
```

Known engine limits, stated rather than left to be discovered:

- **Selenium cannot use an authenticated remote CDP endpoint** — chromedriver's
  `debuggerAddress` has nowhere to put a password — and cannot authenticate a
  proxy. It refuses the first and strips-and-warns on the second.
- **pyppeteer is effectively unmaintained**, and its bundled 2018 Chromium does
  not start on a current macOS; set `PYPPETEER_EXECUTABLE_PATH` (see
  [TROUBLESHOOTING.md](TROUBLESHOOTING.md)).
- `--concurrency` and `--fingerprint` are implemented in the Playwright engine;
  the other two accept the flags for parity and say that they ignore them.

## Usage

```bash
# A stock screen, every page it has.
python3 playwright_scraper.py --url "https://www.screener.in/screens/59/magic-formula/" --pages 50

# A sector, three pages, CSV only.
python3 playwright_scraper.py --url "https://www.screener.in/market/IN08/" --pages 3 --format csv

# Start part-way: this fetches site pages 3, 4 and 5.
python3 playwright_scraper.py --url "https://www.screener.in/market/IN08/?page=3" --pages 3

# Through the Scraping Browser API (endpoint in .env, never on the command line).
python3 playwright_scraper.py --url "..."          # with SCREENER_CDP_ENDPOINT set

# Compare two exhaustive runs of the same listing.
python3 diff_runs.py --old it_2026-09-24.json --new it_2026-09-25.json
```

Credentials live in `.env` (copy `.env.example`), never on a command line,
where they are visible to `ps` and land in shell history. Precedence: explicit
flag > exported environment variable > `.env`. `python3 env_config.py` says
what was picked up without printing it.

The Scraping Browser endpoint has the shape
`ws://{login}-zone-scraping_browser-country-{cc}-pid-{profileId}:{password}@cb.2captcha.com:9222`;
`country-` picks the exit and `pid-` is a profile allowing one live
connection. Profile credentials expire, so take fresh ones from your 2Captcha
dashboard rather than from any example.

### All flags

Shared by the three browser engines: `--url --pages --category --format --out
--delay --retries --retry-delay --concurrency --proxy --proxy-file
--proxy-rotate --proxy-shuffle --proxy-block-retries --twocaptcha-key
--captcha-api --solve-captcha --min-score --cdp-endpoint --allow-empty
--dump-html --headless/--headful --fingerprint --fp-tags --fp-country`. Run
any script with `--help` for the details. `scraper_api_client.py` takes
`--key`, `--cdp-url` and `--wait-text/--wait-element/--wait-state` instead of
the browser flags.

There is deliberately **no `--limit`**: the page offers "Results per page 10
/ 25 / 50", but `?limit=10` and `?limit=50` both returned 25 rows to an
anonymous visitor on 2026-09-25. A flag that changes nothing is a setting that
looks configurable and is not.

## Traps that look like bugs

- **Prices and ranks move between runs** during Indian market hours, and a
  sector page is sorted by market capitalisation, so neighbours swap places.
  Compare runs by `sku`, which `diff_runs.py` does.
- **An out-of-range page is a real page.** `?page=8` of a 7-page listing
  returns page 7's rows with HTTP 200. The scraper reads which page the site
  says it served, so this ends the listing instead of duplicating it.
- **A `window` run is a sample, not the listing.** `diff_runs.py` annotates
  added/removed from a window run as entered-window / left-window, and refuses
  outright to diff two different listings or sort orders.
- **`ticker` can be a number** — that is the site's own URL for that company.

## Verification

- **Offline:** `python3 smoke_test.py` — no network, no key, no browser
  needed. It reads `fixtures_generated.json`: real captures, trimmed,
  scrubbed of the session's csrf token and of a screen author's name, and
  each verified to parse identically to its untrimmed original. `pytest`
  runs the same suite. CI runs it on Python 3.9 and 3.12, once per engine in
  its own virtualenv, and builds the Docker image.
- **Live:** `canary.yml` runs a real 3-page scrape daily, **ungated** — this
  site needs no credential, so a canary that could pass without one is not
  gated on one — and asserts ranks 1–75, INR on every row and market cap on
  every row. Whether GitHub's datacentre runners are served had not been
  measured when this was written; an exit 3 from a bare runner is reported as
  a notice, not a failure.

## Legal

Scrape responsibly. screener.in's [robots.txt](https://www.screener.in/robots.txt)
on 2026-09-25 disallowed `/user/*`, `/*?q=`, `/*?sort=`, `/*?limit=`,
`/*?page=` and `/company/source/quarter/*` for all user agents — which covers every page after the first of any
listing. Read it, and the site's [Terms](https://www.screener.in/guides/terms/),
before running multi-page jobs; whether and how you comply is your decision
and your responsibility as the operator. Rate-limit your requests (`--delay`),
collect only publicly available data, and note that the data is provided to
screener.in by a third party (the footer credits C-MOTS Internet Technologies).
This project is provided as-is, for research and legitimate use.

## Licence

MIT — see [LICENSE](LICENSE).
