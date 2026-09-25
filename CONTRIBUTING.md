# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own `passed / failed / skipped` counts, and lists both the
individual checks it had to skip and any group skipped because an engine
library is absent. A check whose input is missing must call `skip()`, never
`check(label, True)` — a skip asserted as a pass is indistinguishable in the
output from a check that actually ran, and it inflates the count the README
used to quote.

The fixtures are `fixtures_generated.json`, built by `make_fixtures.py` from
raw captures in `captures/` — which is NOT in the repository: a capture
carries the session that fetched it (a csrf token) and, on a /screens/ page,
the screen author's name. `make_fixtures.py` trims each capture, scrubs those
values, and refuses to write a fixture that parses differently from its
untrimmed original. To add one, save a page with
`--dump-html captures/<name>.html`, list it in `SOURCES`, and run
`python3 make_fixtures.py`.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines (and the Scraper API's
`x-debug` header before logging it), but two things are **not** masked: raw
HTML dumps and your shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

screener.in changing its markup is the normal way this stops working, and it
has its own issue template. What the parser reads, so a report can say which
part moved: result rows are `<tr data-row-company-id="...">`; a column is
identified by its header's `data-tooltip` and unit, never by position; and
`<div data-page-info>` ("164 results found: Showing page 1 of 7") decides
pagination. `--dump-html PATH` writes the exact bytes the parser was given,
on success as well as failure.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions reading `fixtures_generated.json` — no pytest, no
conftest. Copy the nearest existing check and edit it. And **break your fix
once on purpose** and watch the suite go red through the check you meant: a
guard that cannot fail passes a green suite perfectly.

Properties in this repo exist because they were once absent somewhere in this
family and cost real time. Tests pin them, so a PR that breaks one fails
rather than silently regressing:

- **An out-of-range page is detected from what the site says it served.**
  `?page=8` of a 7-page listing answers HTTP 200 with page 7's real rows; the
  page's own "Showing page 7 of 7" is what ends the listing. And a run plans
  its page count against the site's own "of 7" rather than walking off the
  end.
- **Pagination is VERIFIED at runtime.** Every multi-page run checks that page
  2 carries rows page 1 did not; if it does not, the run stops and reports
  `partial` rather than a complete-looking result holding one page.
- **A column is read by its name and unit, never by position.** A header in a
  different unit goes to `extra_metrics` under its own name instead of into a
  typed column that promises a unit it does not have.
- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` the site says the listing is empty, `5` the content
  was never obtained, `6` partial run. All four engines produce the same code
  for the same situation because they share `finish_run`.
- **Rows are merged in PAGE order and deduplicated by the site's own company
  id**, so a concurrent run produces the same rows as a sequential one.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the classifier and
the CLI contract against real captures. If yours genuinely needs screener.in,
say in the PR what you ran, from what kind of connection (residential,
datacentre, Scraping Browser), and what you got. Prices move during Indian
market hours, so compare two runs by company id and structure, not by price.

Do not add anything that logs in or submits the registration form. This
project reads what the site serves an anonymous visitor, and deliberately
nothing else.

## Scope

This repo scrapes **public result tables** on screener.in — screens and sector
listings. Out of scope: anything behind a login, anything that submits a form,
and anything that defeats a protection rather than passing it the way an
ordinary browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
