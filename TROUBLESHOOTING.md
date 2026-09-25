# Troubleshooting

Roughly in the order people meet them. Every number here was measured on the
date given, from an ordinary residential connection unless stated otherwise —
and every one of them describes a living site, so treat it as "what was true
then", and re-measure before relying on it.

## Exit 3 — "redirected to the registration/login page"

screener.in answered with its sign-up page instead of the listing. That is
not a captcha (the page carries no widget — nothing for any solver) and not
a refusal status (it is HTTP 200). What the evidence in this repo shows:

* On 2026-09-25, plain `curl` from a residential connection was served
  `/screens/59/magic-formula/`, `/screens/71064/all-stocks/` and page 220 of
  that screen in full, with no cookie, no key and no proxy.
* Captures taken on 2026-08-23/24 by this repo's first version — through the
  Scraping Browser API and through the Scraper API — landed on `/register/`
  for `/screens/` URLs. `/market/` sector pages were not redirected in any
  capture.

So the exit is the likely variable. Try, in order: no `--cdp-endpoint` from an
ordinary connection; then `--proxy` with a residential Indian exit. If you do
use the Scraping Browser, a different `country-` in the login is the thing to
change.

## Exit 3 — "not built by the site"

The response references screener.in's asset host (`cdn-static.screener.in`)
fewer than 5 times — a page served to the browser without a navigation
error, since a proxy that refuses the connection is caught earlier as
`proxy_failed`; every page the site serves — listings, its sign-up page,
even its 404 — references it 25 times or more. The usual cause is Chromium's
own network-error page, which carries `www.screener.in` in its `<title>` and
so reads as a real page to any text check. It means the exit failed: check
the proxy.

## Fewer pages than `--pages`

Read `<out>.meta.json`. If `stop_reason` is `listing_exhausted` and `coverage`
is `exhaustive`, the run fetched every page the site has: it reads the site's
own "Showing page 1 of 7" and never plans past the last page. Asking for 50
pages of a 7-page sector costs 7 fetches.

This matters more than it looks, because screener.in does not fail when asked
for a page past the end: `?page=8`, `?page=999` and `?page=0` of a 7-page
listing all answered HTTP 200 with page SEVEN's real rows (measured
2026-09-25). The page's own statement of which page it served is the only
thing that tells the two apart, and it is what this scraper reads.

If `stop_reason` is `pagination_not_addressable`, page 2 carried nothing page
1 did not already have — the `?page=` convention has changed. Please open a
"Site changed" issue with the URL.

## "cannot read ...: /market/ is the industries overview"

Refused up front, with the reason, because a hub page returning 0 rows reads
as a broken scraper. This repo reads two page kinds:

* a stock screen — `https://www.screener.in/screens/{id}/{slug}/`
* a sector or industry — `https://www.screener.in/market/IN08/`,
  `.../IN08/IN0801/IN080101/`

`/market/` itself (a table of industries), a company page, the list of
screens, and `/screens/raw/?query=...` (HTTP 404 to an anonymous visitor on
2026-09-25) are refused, each with its own reason.

## Exit 4, and the output file was not written

A run that finds nothing writes nothing, so a failure cannot overwrite last
night's good data with `[]`. Exit 4 means the site itself said "0 results
found" — and only that: `--allow-empty` writes an empty file for that case
and no other. No such listing was found to capture while this repo was
written, so that path is tested on a synthetic page and is unmeasured on the
live site.

## Exit 5 — the content was never obtained

A navigation timeout, a dead proxy (`stop_reason: proxy_failed` — the exit,
not the site), the site's own 404 (check the URL), an HTTP 5xx, a `--url`
whose `?page=` is past the last page (`start_page_out_of_range`), or a page
that carried a results table this code could not read. Nothing can be concluded
about the listing from such a run, which is why it is not exit 4. The log says
which page and why; `--dump-html dump.html` writes what the parser was given.

## A column is empty, and `extra_metrics` has something like it

By design. A column is identified by the header's own metric name AND unit —
"Market Capitalization" in "Rs.Cr." fills `market_cap_cr`. If the site shows
it in another unit, the value goes into `extra_metrics` under its own name
and unit instead, and the run logs a warning naming the column. A number
written into a column whose name promises a different unit is worse than an
empty column.

A screen can also show columns beyond the nine every page kind has
(Magic Formula adds ROIC, Earnings Yield and Book Value); those are always in
`extra_metrics`.

## Two runs a minute apart disagree on price, market cap and order

That is the market, not the scraper. During Indian market hours the price
moves between fetches, and a sector page is sorted by market capitalisation,
so neighbours can swap `rank` between two runs. Measured 2026-09-25: three
engines run within a minute returned the same 75 companies with identical
per-company quarterly figures, and two companies had swapped places. Compare
runs by `sku` (the site's company id), which is what `diff_runs.py` does.

## `ticker` is sometimes a number

The ticker column is what screener.in puts in the company's URL: letters
(`TCS`) or six digits (`532015`). Both are the site's own spelling; this repo
does not translate one into the other.

## `consolidated` differs between two companies in one table

The table links some companies to their consolidated figures and others to
their standalone ones (71 of 108 measured rows were consolidated). The
column records which view the row's figures link to.

## Exit 2 — "the proxy exit … cannot be used"

The run stopped before launching a browser, because one small request through
that exit failed in a way another attempt cannot fix. The usual cause is the
one the message names: the password was rotated on the provider's side and
`.env` still holds the old one. `407` in the detail means exactly that. A
slow exit only warns — the target may be what is slow.

## Selenium: refused with a Scraping Browser endpoint

Expected, and refused up front with the reason. Chromedriver's
`debuggerAddress` takes a bare `host:port` and has nowhere to put a password,
while Playwright's `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take
a full `ws://user:pass@host:port`. Use either of those engines instead.
Selenium also cannot authenticate a `--proxy`: the credentials are stripped
and the run warns rather than pretending.

## pyppeteer: "Browser closed unexpectedly"

pyppeteer downloads a Chromium from 2018 and launches that. On a current macOS
it does not start. Point it at a browser that does:

    PYPPETEER_EXECUTABLE_PATH="$HOME/Library/Caches/ms-playwright/chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" \
      python3 puppeteer_scraper.py --url "..."

An environment variable rather than a flag, so the three engines keep exactly
the same flag set.

## A captcha is reported on a page that clearly has none

screener.in configures no captcha anywhere this repo has looked. One cause is
known and handled: the Scraping Browser's auto-solve extension injects 16
captcha hunter scripts into every page it loads, one carrying
`data-ts-input="cf-turnstile-response"`, plus an empty `<captcha-widgets>`
mount. The captcha detector strips extension script tags first and reads the
mount by whether it has content, and the offline suite pins both against a
real capture taken through the Scraping Browser.

## `--fingerprint` seems to do nothing

Run `python3 fingerprint_client.py --explain`, with the key exported as
`TWOCAPTCHA_KEY`. It prints, field by field, which response key each setting
came from and which settings could not be applied at all. If it answers
**403**, the key is not subscribed to fingerprints — a separate product from
captcha solving. `--fingerprint` is implemented in the Playwright engine only.
