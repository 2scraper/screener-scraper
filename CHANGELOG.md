# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) as closely as a command-line
toolkit can: a patch release means fixes, not that every flag and default is
frozen, and a patch that changes behaviour for an existing user leads its
notes with a warning saying so.

## [Unreleased]

### Added

- **`http_scraper.py`, a plain-HTTP engine.** It needs no browser and no key.
  The table is in the served HTML, so a GET is enough.
  - Measured on 2026-09-28, page 1: 1.2 s and 47 MB RSS, against 2.9 s and
    150 MB for Playwright (plus Chromium's own processes). The 25 rows are
    identical to Playwright's.
  - How it fetches:
    - one session, with separate connect and read timeouts;
    - bounded retries of a connection error, a 429 or a 5xx, with a
      jittered exponential backoff and `Retry-After` honoured (capped);
    - a refusal is never retried from the same address;
    - an honest User-Agent;
    - `--proxy` / `--proxy-file`;
    - sequential only.
  - It is optional: Playwright stays the default.
- **`listing_run.py`: the page loop the two browserless engines share.** The
  Scraper API client now runs on it too, so the HTTP engine is not a fourth
  copy of that loop. A transport is `fetch(args, url) -> (html, status,
  final_url)` and does its own transient-fault retries. The Scraper API
  client now reports the last page it fetched as `final_url`, not the start
  URL.

### Changed

- **Every download CI and the image make is pinned.**
  - Actions are pinned by commit SHA, with the release in a comment;
    Dependabot proposes updates.
  - CI, the canary and the Docker image install only the hashed
    `requirements*.lock` files, with `--require-hashes`. The `.txt` files stay
    as the loose spec.
  - `audit.yml` runs `pip-audit` over every lock on each change and weekly.
    The pyppeteer lock's five urllib3 1.x advisories (pyppeteer needs
    `urllib3<2`) are ignored by ID and explained in the README.
  - Workflows run with a read-only token.
  - Ported from lg-scraper 0.2.1; the locks resolve to the same pins.

### Fixed

- **A refused Fingerprint API crashed the run with a traceback and exit 1.**
  It now exits 5, the remote-API code, with the reason and no traceback.
  The case is real: on 2026-09-28 a key with a working captcha-solving
  balance got 403 from `/fingerprint/random`, because fingerprints are a
  separate subscription.
- The real-page captcha check added in 0.1.1 reported a script error as a
  skip ("no browser"). An error from the script itself is now a failure.

## [0.1.1] — 2026-09-28

Fixes from a second pass over a third-party audit of 0.1.0.

> **Behaviour changes for an existing user:** the daily canary now fetches
> page 1 only, and every engine WARNS when a run requests `?page=` URLs,
> which screener.in's robots.txt disallows. `.meta.json` gains fields
> (`schema_version`, `run_id`, `engine`, `files`); none was removed or
> renamed. `diff_runs.py` now refuses a JSON that does not match its own
> sidecar's digest.

### Security

- **The CI secret scan judged the LINE, not the credential.** A line was
  exempt as soon as it contained `***` or `user:pass` anywhere, so a real
  `…user:password…@` login or a real URL next to a masked one passed. The
  allowlist is now matched exactly against each URL's userinfo, and the scan
  also catches token-only `wss://TOKEN@` endpoints, upper-case 32-hex keys,
  written-out bearer tokens, and no longer exempts a hex key on a line that
  merely says "shares" or "shape".
- **`python3 env_config.py` printed a CDP endpoint or proxy in clear** when
  its value had no `@` — a token in the query string. Credential variables
  are now hidden by name, whatever their value looks like.
- **Raw driver exceptions reached warning logs** (a retry, a failed
  screenshot, a content read). Every CLI now installs a log filter that
  redacts each record's final message and traceback.
- **A JSON-escaped URL (`ws:\/\/login:secret@…`) was not masked.**
- **The canary uploaded unscrubbed page dumps**, with the anonymous session's
  csrf token, as public artefacts. They are scrubbed first now.

### Changed

- The canary requests page 1 only; see the warning above.
- The Scraper API client retries a connection error (no task ran, nothing
  billed) and reads the policy table's `retry` rather than hard-coding
  "blocked"; `PagePolicy.retry` had no reader at all. The suite now asserts
  every policy field has one.
- The concurrent Playwright path applies the same data-based stop as the
  sequential one: a page adding no new row ends the listing there.
- README: Quick start fetches page 1; the Scraper API client's different
  `--retries`/`--retry-delay` meanings are documented.

### Fixed

- **Runtime captcha detection never ran, in any engine.** The discovery
  script began ` => {` with no parameter list, a SyntaxError swallowed at
  debug level; Selenium additionally returned the function instead of calling
  it. Both fixed, the failure is now a warning, and the suite parses every
  shipped script with `node --check` and runs discovery in a real page.
- **An explicitly rendered reCAPTCHA v2 widget was classified as v3** by the
  HTML detector, and a v2 task sent as v3 is `ERROR_CAPTCHA_UNSOLVABLE`.
  Found by the first live solve (2026-09-28, 2Captcha's reCAPTCHA v2 demo
  page); after the fix both the v2 and v1 APIs solved it and the page's own
  server-side check answered `"success": true`.
- The offline suite really slept 20 of its 22 seconds (two blocked scenarios
  waiting out the default `--retry-delay`). It now runs in about a second,
  and fails if any check sleeps a second or more.

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
