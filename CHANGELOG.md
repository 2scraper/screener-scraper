# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) as closely as a command-line
toolkit can: a patch release means fixes, not that every flag and default is
frozen, and a patch that changes behaviour for an existing user leads its
notes with a warning saying so.

## [0.1.0] — 2026-09-25

First release on the 2scraper family core, replacing an unpublished first
version of this scraper.

> **For anyone who used that first version:** it concluded that every
> `/screens/` URL is gated behind registration. That was measured only
> through the Scraping Browser API and the Scraper API. From an ordinary
> residential connection on 2026-09-25, plain HTTP was served three screens
> in full. The registration wall is now classified as blocked (exit 3) with
> advice, and never read as an empty screen.

### Added

- Result tables of any stock screen (`/screens/{id}/{slug}/`) and any sector
  or industry listing (`/market/IN.../`), to JSON and CSV, through four
  engines sharing one parser, one page classifier and one exit-code mapping:
  Playwright (primary), pyppeteer, Selenium and the 2Captcha Scraper API.
- A fixed row schema — the family prefix, then `rank`, `ticker`,
  `consolidated`, nine typed metric columns — plus `extra_metrics` for any
  column a screen adds, keyed by the site's own metric name and unit.
  Columns are read by name and unit, never by position.
- Pagination planned against the site's own "Showing page X of Y", and an
  out-of-range page detected from the page number the site says it served
  (`?page=8` of 7 is answered with page 7's real rows).
- A `<out>.meta.json` sidecar with `status`, `coverage` (exhaustive /
  tail / window — `tail` for a run that reached the end from a later
  `?page=`), `start_page`, the site's `total_results` and `pages_available`,
  and `rank_gaps` computed from the site's own global row numbers.
- Exit 5 for every run that never obtained its content — a timeout, a dead
  proxy (`proxy_failed`), a 404 or 5xx, a start page past the end — and a
  blocked or failed run never writes output, `--allow-empty` included.
- Page states for the registration/login wall, the site's own 404, and
  Chromium's own network-error page.
- `diff_runs.py`, which refuses to compare different listings or sort orders
  and annotates window runs.
- An offline suite over real, scrubbed captures (`fixtures_generated.json`,
  built by `make_fixtures.py`), including one fetched through the Scraping
  Browser with its 16 injected extension scripts.

### Not in this release, said so rather than implied

- The Scraper API engine, `--fingerprint` and the captcha solver were not run
  against the live 2Captcha APIs: no key was available when this was written.
  They are exercised offline against stubbed responses.
- This repo does not implement `RecaptchaV2EnterpriseTaskProxyless` or the
  Cloudflare Challenge-page form of `TurnstileTaskProxyless`. No captcha is
  configured anywhere on screener.in as measured, so neither was needed.
- No listing reporting "0 results found" was found to capture; that state is
  tested on a synthetic page only.
