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
from typing import List, Optional, Tuple

import requests

import cli_types
import env_config
import page_flow
from output_writer import EXIT_API_ERROR, finish_run, merge_pages
from product_parser import parse_products
from proxy_pool import redact_secret_patterns

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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


def _fetch_page(args, page_num: int, url: str):
    """Fetch and classify one page; returns (state, products) or (None, []).

    A challenge page is not a final answer, so a blocked response is retried
    `--retries` times before the run concludes anything — each attempt is a
    separate billable task, which is why the default is low.
    """
    for attempt in range(1, max(1, args.retries + 1) + 1):
        try:
            html, upstream = fetch_html(args, url)
        except requests.RequestException as e:
            logger.error("Network error talking to the Scraper API: %s",
                         redact_secret_patterns(str(e)))
            return None, []
        except RuntimeError as e:
            logger.error("%s", e)
            return None, []

        if args.dump_html:
            path = args.dump_html if args.pages == 1 else f"{args.dump_html}.page{page_num}"
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info("Raw HTML written to %s", path)

        state = page_flow.classify(html, status_code=upstream, url=url,
                                   requested_page=page_num)
        logger.info("Page %d is %s.", page_num, state)

        if state.policy.blocked and attempt <= args.retries:
            logger.warning("Blocked (%s) on attempt %d — retrying in %ds. A "
                           "challenge page is not a final answer.",
                           state.reason, attempt, args.retry_delay)
            time.sleep(args.retry_delay)
            continue

        if state.policy.blocked:
            dump = f"{args.out}_page{page_num}_debug.html"
            with open(dump, "w", encoding="utf-8") as f:
                f.write(html)
            logger.error("Blocked before parsing (%s) — saved the response to "
                         "%s. This is exit 3, distinct from a listing that "
                         "genuinely has no rows (exit 4). %s", state.reason, dump,
                         page_flow.block_advice(state, has_pool=False))
            return state, []

        if not state.policy.usable:
            logger.error("Page %d: %s (%s).", page_num, state.reason, url)
            return state, []

        if state.state in (page_flow.EMPTY, page_flow.EXHAUSTED):
            return state, []

        products = parse_products(html, url, category=args.category, page=page_num)
        logger.info("Parsed %d row(s) from page %d.", len(products), page_num)
        short = page_flow.shortfall(state.total_results, page_num, len(products))
        if short:
            logger.warning("%s.", short)
        if not products:
            dump = f"{args.out}_page{page_num}_debug.html"
            with open(dump, "w", encoding="utf-8") as f:
                f.write(html)
            logger.warning("0 rows parsed from a page classified as %s — "
                           "saved the raw response to %s.", state.state, dump)
        return state, products
    return None, []


def _page_ok(state, products) -> bool:
    """The page answered the question: usable, not blocked, and not a parse failure."""
    return (state is not None and state.policy.usable
            and not _parse_failed(state, products))


def _parse_failed(state, products) -> bool:
    """Classified as carrying rows, yet nothing parsed out of it.

    page_flow reports the real end of a listing as EMPTY or EXHAUSTED, both of
    which carry policy.complete. A page that is neither of those, is not a
    challenge, and still yields no rows is a parse or schema failure — and
    treating it as the end of the listing is how a run holding half the
    catalogue reports itself complete.
    """
    return (state is not None and not products and state.policy.usable
            and not state.policy.complete)


def _stop_reason_for(state, products) -> Optional[str]:
    """Why a fetched page ends the run as a failure, or None if it does not."""
    if state is None:
        return "api_error"
    if state.policy.blocked:
        return f"blocked_{state.vendor}"
    if not state.policy.usable:
        return f"page_{state.state}"
    if _parse_failed(state, products):
        logger.error("0 rows parsed from a page classified as %s (%s) — that "
                     "is a parse failure, not the end of the listing.",
                     state.state, state.reason)
        return "content_unparsed"
    return None


def scrape(args) -> int:
    outcomes: List[Tuple[int, object, list]] = []
    blocked = False
    stop_reason = "completed"
    total_results = None
    pages_available = None
    addressable = None
    start = args.start_page

    state, products = _fetch_page(args, start, args.url)
    outcomes.append((start, state, products))
    if state is not None:
        total_results = state.total_results
        pages_available = state.pages_available
    planned = page_flow.plan_pages(args.pages, start, pages_available)
    if planned < args.pages and state is not None and state.policy.usable:
        logger.info("The site states %s page(s) for this listing, so this run "
                    "fetches %d rather than the %d asked for.",
                    pages_available, planned, args.pages)

    failure = _stop_reason_for(state, products)
    if failure:
        stop_reason = failure
        blocked = bool(state and state.policy.blocked)
    elif state.policy.complete:
        stop_reason = ("no_results" if state.state == page_flow.EMPTY
                       else "listing_exhausted")
    elif planned > 1:
        seen = {p.sku for p in products if p.sku}
        first_products = products
        for run_page in range(2, planned + 1):
            page_num = page_flow.site_page(start, run_page)
            time.sleep(args.delay)
            state, products = _fetch_page(args, page_num,
                                          page_flow.url_for(args.url, start, run_page))
            outcomes.append((page_num, state, products))
            failure = _stop_reason_for(state, products)
            if failure:
                stop_reason = failure
                blocked = bool(state and state.policy.blocked)
                break
            if state.policy.complete or not products:
                stop_reason = "listing_exhausted"
                break
            if run_page == 2:
                # The same addressability check the browser engines run,
                # against the DATA. Page 2 stays in the outcomes, as there.
                addressable = any(p.sku and p.sku not in
                                  {q.sku for q in first_products if q.sku}
                                  for p in products)
                if not addressable:
                    logger.error("Page 2 returned nothing page 1 did not already "
                                 "have — this listing cannot be paged through by "
                                 "URL. Stopping and reporting a PARTIAL run.")
                    stop_reason = "pagination_not_addressable"
                    break
            fresh = [p for p in products if p.sku is None or p.sku not in seen]
            seen.update(p.sku for p in products if p.sku)
            if not fresh:
                logger.info("Page %d added nothing not already seen — treating "
                            "that as the end of the listing.", page_num)
                stop_reason = "no_new_listings"
                break
    if (stop_reason == "completed"
            and page_flow.reached_last_page(start, planned, pages_available)):
        stop_reason = "listing_exhausted"

    all_products, merge_stats = merge_pages(
        (page_num, products) for page_num, _state, products in outcomes)
    if merge_stats.duplicate_skus_across_pages:
        logger.info("Dropped %d row(s) already seen on an earlier page "
                    "(page(s) %s).", merge_stats.duplicate_skus_across_pages,
                    ", ".join(str(n) for n in merge_stats.duplicate_pages))

    ok_pages = [o for o in outcomes if _page_ok(o[1], o[2])]
    failed = [o[0] for o in outcomes if not _page_ok(o[1], o[2])]
    counted = [o[1].total_results for o in sorted(outcomes, key=lambda o: o[0])
               if o[1] is not None and o[1].total_results is not None]
    return finish_run(all_products, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed, total_results=total_results,
                      addressable=addressable, merge_stats=merge_stats,
                      total_results_first=counted[0] if counted else None,
                      total_results_last=counted[-1] if counted else None,
                      pages_available=pages_available,
                      start_url=args.url, final_url=args.url)


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
    p.add_argument("--retries", type=cli_types.positive_int, default=1,
                   help="Extra attempts if a challenge page comes back. Each "
                        "attempt is a separate billable task, so this defaults to 1.")
    p.add_argument("--retry-delay", type=cli_types.non_negative_float, default=10,
                   help="Seconds between retries (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the raw returned HTML, on success as well as failure")
    args = p.parse_args()
    # This client uses --key and --cdp-url rather than --twocaptcha-key and
    # --cdp-endpoint, so the mapping is spelled out instead of defaulted.
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "SCREENER_CDP_ENDPOINT": "cdp_url",
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
