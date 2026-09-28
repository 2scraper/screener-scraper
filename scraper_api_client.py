#!/usr/bin/env python3
"""
screener-scraper — 2Captcha Scraper API edition (fourth engine)
================================================================

A fourth way to run this scraper, managing **no browser and no CDP session of
its own**: it POSTs a URL to 2Captcha's Scraper API
(https://scraper.2captcha.com — a different product from the Scraping Browser
API the other three engines reach over `--cdp-endpoint`), gets HTML back over
plain HTTPS, and feeds that HTML to the same product_parser.

**On this site the browserless path is enough.** The results table is
server-rendered: plain curl with no JavaScript executed got every row of a
sector page, three screens and page 220 of "All Stocks" on 2026-09-25. So
`--wait-element` and friends are available but not needed, and
`requirements.txt` alone is enough to run this engine.

What was NOT measured, and is said so rather than assumed: this repo has no
2Captcha key of its own on the day it was written, so this engine has not
been run against the live API in its current form. A capture taken through
it on 2026-08-23 (by this repo's first version) landed on screener.in's
REGISTRATION page for a /screens/ URL; the classifier reports that as
blocked (exit 3) with advice, never as an empty screen.

API surface used (per https://2captcha.com/scraper/scraper-api/api)
-------------------------------------------------------------------
  POST https://scraper.2captcha.com/tasks/sync
    Authorization: Bearer <API_KEY>
    Content-Type: application/json
    {"task_type": "scrape", "url": ..., "data_format": "raw",
     "format": "json", "timeout": 1..120,
     "waitFor": {...},                     # an OBJECT — see _build_wait_for
     "cdpurl": "ws://user:pass@host:port"  # optional
    }
  -> 200 {"status": "<verdict>", "http_code": <target status>,
          "headers": {...}, "body": "<!DOCTYPE html>..."}

Two corrections a sibling repo measured on 2026-09-23 and this client
carries (inherited, not re-measured here — see the note above): `waitFor` as
a JSON-encoded STRING is refused with HTTP 422 and still billed, while the
object form is accepted; and the target's HTTP status is `http_code`, while
`status` is the API's own verdict string.

Usage
-----
    TWOCAPTCHA_KEY=... python3 scraper_api_client.py \
        --url "https://www.screener.in/market/IN08/IN0801/IN080101/" \
        --pages 3 --out it_software

Requires: pip install -r requirements.txt
          (no playwright/selenium/pyppeteer needed for this engine)
"""

import argparse
import json
import logging
import sys
import time
from typing import Optional, Tuple

import requests

import cli_types
import env_config
import listing_run
from listing_run import FetchFailed
from proxy_pool import install_log_redaction, redact_secret_patterns

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
install_log_redaction()
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s.
MAX_API_TIMEOUT = 120



def _build_wait_for(args) -> Optional[dict]:
    """`waitFor` as a JSON OBJECT, or None.

    The flippa-era client this was ported from built a double-encoded
    STRING, and its docstring said the API wanted one. A sibling measured
    the opposite on 2026-09-23: the string form answers HTTP 422
    ("params.waitFor must be an object") and is still billed. Not needed on
    screener.in, whose table is in the served HTML; kept for a future page
    kind or a run routed through `--cdp-url`.
    """
    if args.wait_text:
        return {"text": args.wait_text}
    if args.wait_element:
        return {"element": args.wait_element, "checkVisible": True}
    if args.wait_state:
        return {"state": args.wait_state}
    return None


def fetch_html(args, url: str) -> Tuple[str, Optional[int]]:
    """One Scraper API task. Returns (html, upstream_status)."""
    payload = {
        "task_type": "scrape",
        "url": url,
        "data_format": "raw",    # we want HTML; product_parser does the rest
        "format": "json",        # so we get {"status", "headers", "body"}
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }
    wait_for = _build_wait_for(args)
    if wait_for:
        payload["waitFor"] = wait_for
        logger.info("waitFor: %s", json.dumps(wait_for))
    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    redact_secret_patterns(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}",
                 "Content-Type": "application/json"},
        json=payload,
        # More headroom than the API-side task timeout, so a task that
        # legitimately runs the full 120s does not look like a network failure.
        timeout=min(args.timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its per-task metadata (price, timings, status) in an
    # x-debug header — the only place the real cost of the call shows up.
    # REDACTED before it is logged: the header echoes the task back, so a run
    # through a credentialled --cdp-url would print that endpoint's password
    # (a family-wide finding of 2026-09-21: 27 repos logged it verbatim).
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", redact_secret_patterns(debug))

    if resp.status_code != 200:
        # 422 = the task ran and errored (an unreachable cdpurl does this);
        # 402 = out of balance; 408 = the sync wait was exceeded.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: "
            f"{redact_secret_patterns(resp.text[:500])}")

    body = resp.json()
    html = body.get("body") or ""
    # The TARGET's status is `http_code`; `status` is the API's own verdict
    # string. Reading `status` handed "success" to the classifier and hid a
    # target's 403 in the sibling this was measured on. `status` is kept as a
    # fallback only when it is an int, for an older response shape.
    upstream = body.get("http_code")
    if not isinstance(upstream, int):
        legacy = body.get("status")
        upstream = legacy if isinstance(legacy, int) else None
    logger.info("Upstream page status %s, %d bytes of HTML.", upstream, len(html))
    return html, upstream


def _fetch(args, url: str):
    """This client's transport for listing_run: one Scraper API task.

    Retries only a connection error — the request never reached the API, so
    no task ran and nothing was billed. A read TIMEOUT is not retried: the
    task may have run and been billed. A page worth fetching again is
    listing_run's decision (`refetch_on_policy`), taken from the policy table.
    """
    for attempt in range(1, args.retries + 2):
        try:
            html, upstream = fetch_html(args, url)
            return html, upstream, None
        except requests.ConnectionError as e:
            if attempt <= args.retries:
                logger.warning("Could not reach the Scraper API (%s) — "
                               "retrying in %.1fs.",
                               redact_secret_patterns(str(e)), args.retry_delay)
                time.sleep(args.retry_delay)
                continue
            raise FetchFailed("Network error talking to the Scraper API: "
                              + redact_secret_patterns(str(e))) from None
        except requests.RequestException as e:
            raise FetchFailed("Network error talking to the Scraper API: "
                              + redact_secret_patterns(str(e))) from None
        except RuntimeError as e:
            raise FetchFailed(str(e)) from None
    raise FetchFailed("Scraper API: no attempt made")  # unreachable: retries >= 0


def scrape(args) -> int:
    # `fetch_html` is looked up at call time, so a test that replaces the
    # module attribute replaces what the run fetches with.
    return listing_run.run(args, lambda a, u: _fetch(a, u), engine="scraper_api",
                           refetch_on_policy=True, logger=logger)


def parse_args():
    p = argparse.ArgumentParser(
        description="screener.in scraper — 2Captcha Scraper API edition (no local browser)")
    # NOT required as a flag: a key on the command line is visible to anything
    # that can run `ps` and lands in shell history.
    # No default read from os.environ here: env_config.apply fills it below
    # with the family's precedence (flag > exported variable > .env), and a
    # default taken at parse time would make an exported variable look like
    # an explicit flag.
    p.add_argument("--key", default=None,
                   help="2captcha.com API key, sent as a Bearer token. Better "
                        "set as TWOCAPTCHA_KEY in the environment or .env — a key "
                        "on the command line lands in shell history.")
    p.add_argument("--url", default=None,
                   help="A screener.in screen or sector listing URL. Required "
                        "unless SCREENER_URL is set.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Defaults to the "
                        "listing's own heading, read from the page.")
    p.add_argument("--pages", type=cli_types.positive_int, default=1,
                   help="Number of pages to fetch (25 rows each).")
    p.add_argument("--delay", type=cli_types.non_negative_float, default=1.0,
                   help="Delay between pages, seconds (default 1.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="screener_results", help="Output file prefix")
    p.add_argument("--timeout", type=cli_types.positive_int, default=60,
                   help=f"API-side task timeout in seconds (1-{MAX_API_TIMEOUT}, default 60)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session over "
                        "CDP (the API's `cdpurl` parameter). Not needed on this "
                        "site — the table is in the served HTML.")
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait-text", default=None,
                      help="Wait until this string appears on the page.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible, e.g. "
                           "'tr[data-row-company-id]'. Not needed here: the "
                           "table is in the served HTML.")
    wait.add_argument("--wait-state", choices=["load", "domcontentloaded"], default=None,
                      help="Wait for a page load state instead of specific content")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were parsed.")
    p.add_argument("--retries", type=cli_types.non_negative_int, default=1,
                   help="Extra attempts if a challenge page comes back. Each "
                        "attempt is a separate billable task, so this defaults to 1.")
    p.add_argument("--retry-delay", type=cli_types.non_negative_float, default=10,
                   help="Seconds between retries (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the raw returned HTML, on success as well as failure")
    args = p.parse_args()
    # This client uses --key and --cdp-url rather than --twocaptcha-key and
    # --cdp-endpoint, so the mapping is spelled out instead of defaulted.
    # SCREENER_CDP_ENDPOINT is deliberately NOT mapped onto --cdp-url. It is
    # the browser engines' endpoint, and filling it in here would route
    # every Scraper API task through the Scraping Browser too — two products
    # billed per page, through the exit the captures saw the registration
    # wall from — without the user ever typing --cdp-url.
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "SCREENER_URL": "url",
    })
    cli_types.finish_args(p, args, logger)
    return args


def main() -> int:
    args = parse_args()
    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export "
                     "TWOCAPTCHA_KEY.")
        return 2
    return scrape(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception:  # noqa: BLE001 — a traceback is a log, as in the twins
        import traceback
        logger.error("Crashed:\n%s", redact_secret_patterns(traceback.format_exc()))
        sys.exit(1)
