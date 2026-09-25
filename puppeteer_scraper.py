#!/usr/bin/env python3
"""
screener-scraper — pyppeteer edition
=====================================

The same scrape as playwright_scraper.py, driven through pyppeteer. Kept for
parity, not because it is better: **pyppeteer is effectively unmaintained**
and its own README points at Playwright. Use this when you already have it, or
to check that a result is not an artefact of one driver.

Parity is the point: this engine must agree with its twins on
exit codes, run status, and whether a run crashes or spends money. It shares
`page_flow.py` for what a page IS, `product_parser.py` for what a row
contains, and `output_writer.finish_run` for the status/exit mapping, so the
three cannot quietly disagree.

Engine-specific notes:

  * **Proxy credentials never reach the command line.** Chromium's
    `--proxy-server` switch is argv, readable by anything that can run `ps`,
    so the host and port go there and the credentials go through
    `page.authenticate()`.
  * **Its bundled Chromium is from 2018.** pyppeteer downloads and launches
    that one by default, and on a current macOS it dies with "Browser closed
    unexpectedly" before the first fetch. Set `PYPPETEER_EXECUTABLE_PATH` to a
    browser that runs — the Chromium `playwright install chromium` already
    fetched will do — and this engine uses it.
  * **`connect()` gets an explicit timeout.** pyppeteer's own API provides
    none, and a quiet endpoint otherwise hangs the run indefinitely — it once
    hung for ten minutes on another site in this family before the wrapper
    below was added.

Usage
-----
    python3 puppeteer_scraper.py \
        --url "https://www.screener.in/market/IN08/IN0801/IN080101/" \
        --pages 3 --out it_software

Requires: pip install -r requirements.txt -r requirements-puppeteer.txt
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

# Imported at MODULE level on purpose: the offline suite guards its
# pyppeteer-specific checks behind `import puppeteer_scraper`, and an engine
# that imports its driver lazily imports cleanly with the driver absent — so
# the group never skips, and the CI job whose whole purpose is to fail on an
# unexpected skip cannot catch a broken import.
from pyppeteer import connect, launch
from pyppeteer.errors import NetworkError, PageError, TimeoutError as PPTimeout

import cli_types
import env_config
import page_flow
from captcha_solver import (CAPTCHA_DISCOVERY_JS, INJECT_TOKEN_FN,
                            challenge_from_discovery, detect_in_html,
                            reconcile_detections, solve)
from output_writer import failure_stop_reason, finish_run, merge_pages
from product_parser import SELECTORS, parse_products
from proxy_pool import (ROTATE_MODES, ProxyError, check_exit_or_raise,
                        from_args as proxy_pool_from_args, mask,
                        redact_secret_patterns, to_pyppeteer)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

# --- the whole of this engine's site knowledge, identical to its twins' -----
ITEM_CARD_SELECTOR = SELECTORS["item_card"]
MIN_CARD_MATCHES = 5
RENDER_WAIT_MS = 15000
NEXT_PAGE_SELECTOR = None
# ---------------------------------------------------------------------------

CONNECT_TIMEOUT_S = 30
GOTO_TIMEOUT_MS = 60000


@dataclass
class PageOutcome:
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    state: Optional[page_flow.PageState] = None
    load_failed: bool = False
    # Classified as carrying rows, yet nothing parsed out of it — a
    # parser/schema problem, never the end of the listing. See the twins.
    parse_failed: bool = False
    lost: bool = False
    # The exit (proxy) failed, as Chromium reported it — not a timeout.
    proxy_failed: bool = False
    # The fetch raised something no branch expected. Recorded as a failed
    # page, so the rows already gathered are still written.
    raised: bool = False

    @property
    def ok(self) -> bool:
        return (not self.load_failed and not self.parse_failed
                and not self.lost and self.state is not None
                and self.state.policy.usable)

    @property
    def complete_here(self) -> bool:
        return self.state is not None and self.state.policy.complete


_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    text = str(exc)
    return next((m for m in _PROXY_ERROR_MARKERS if m in text), "")


async def _open_browser(args, pool):
    """Launch or connect, and return (browser, page).

    A rotation closes this and calls it again — never swaps the proxy under a
    live session, for the reason proxy_pool.py's docstring gives.
    """
    if args.cdp_endpoint:
        logger.info("Connecting to existing browser over CDP: %s",
                    redact_secret_patterns(args.cdp_endpoint))
        # pyppeteer's connect has no timeout of its own — and its failures,
        # like Playwright's, quote the endpoint with its password in them.
        try:
            browser = await asyncio.wait_for(
                connect(browserWSEndpoint=args.cdp_endpoint),
                timeout=CONNECT_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 — re-raised immediately, redacted
            raise RuntimeError(
                f"Could not connect to --cdp-endpoint: "
                f"{redact_secret_patterns(str(e))}") from None
        page = await browser.newPage()
        await _enable_auto_solve(page)
        return browser, page

    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
    credentials = None
    if pool:
        proxy_arg, credentials = to_pyppeteer(pool.current)
        if proxy_arg:
            launch_args.append(proxy_arg)
            logger.info("Using proxy exit %s", mask(pool.current))
    launch_kwargs = {"headless": args.headless, "args": launch_args}
    # pyppeteer downloads a Chromium from 2018 and launches THAT. On a
    # current macOS or a current glibc it dies with "Browser closed
    # unexpectedly" before the first fetch — measured on this machine
    # 2026-09-19, which is also the only way this shows up: the module
    # imports, --help works and the offline suite passes either way.
    # PYPPETEER_EXECUTABLE_PATH points it at a browser that does run, e.g.
    # the Chromium `playwright install chromium` already fetched. An
    # environment variable rather than a flag, so the three engines keep
    # exactly the same flag set.
    executable = os.environ.get("PYPPETEER_EXECUTABLE_PATH")
    if executable:
        launch_kwargs["executablePath"] = executable
        logger.info("Launching the browser at PYPPETEER_EXECUTABLE_PATH=%s",
                    executable)
    browser = await launch(**launch_kwargs)
    page = await browser.newPage()
    if credentials:
        # The password goes through the DevTools protocol, not through argv.
        await page.authenticate(credentials)
    return browser, page


async def _enable_auto_solve(page) -> None:
    """Turn on the Scraping Browser API's own solver for this session."""
    try:
        cdp = await page.target.createCDPSession()
        await cdp.send("Captcha.setAutoSolve",
                       {"autoSolve": True, "options": [{"type": "*"}]})
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
    except Exception as e:  # noqa: BLE001 — any non-2Captcha endpoint lands here
        logger.info("Captcha.setAutoSolve is not available on this "
                    "--cdp-endpoint (%s) — relying on this project's own "
                    "detect+solve instead.", redact_secret_patterns(str(e)))


async def _count_cards(page) -> int:
    """Card count through the protocol — never an evaluated string.

    `querySelectorAll` here is DOM.querySelectorAll over CDP, so it works
    under any Content-Security-Policy.
    """
    try:
        return len(await page.querySelectorAll(ITEM_CARD_SELECTOR))
    except Exception as e:  # noqa: BLE001
        logger.debug("Counting cards failed: %s", e)
        return 0


async def _wait_for_cards(page, minimum: int, timeout_ms: int = RENDER_WAIT_MS) -> int:
    """Wait for `minimum` rows; `>=` because reaching it IS success. See the twin."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        count = await _count_cards(page)
        if count >= minimum or time.monotonic() >= deadline:
            return count
        await asyncio.sleep(0.4)


async def _content(page) -> Optional[str]:
    try:
        return await page.content()
    except Exception as e:  # noqa: BLE001 — a page mid-navigation raises here
        logger.warning("Could not read the page content (%s).", e)
        return None


async def handle_captcha_if_present(page, args, solves: List[int]) -> None:
    """Runs after EVERY navigation, for ANY page — not scoped to one URL.

    `solves` is this page's spend counter; page_flow.solve_budget is the gate.
    """
    html = await _content(page)
    if html is None:
        return
    url = page.url

    # pyppeteer's evaluate is a coroutine, so the discovery script is awaited
    # here and its RESULT is handed to the shared classifier. Wrapping it in a
    # synchronous-looking callable instead would mean calling
    # run_until_complete from inside the loop that is already running, which
    # raises.
    runtime = None
    try:
        info = await page.evaluate(CAPTCHA_DISCOVERY_JS)
        runtime = challenge_from_discovery(info, page_url=url)
    except Exception as e:  # noqa: BLE001 — discovery must never end a run
        logger.debug("Runtime captcha discovery failed: %s", e)

    challenge = reconcile_detections(detect_in_html(html, url), runtime)
    if not challenge:
        return

    state = page_flow.classify(html, url=url)
    if args.solve_captcha == "when-blocked" and not state.policy.may_solve:
        logger.info("%s detected via %s, but the page is %s — not paying to "
                    "solve it.", challenge.kind, challenge.source, state)
        return
    if not page_flow.solve_budget(solves[0]):
        logger.warning("%s detected again, but this page has already spent "
                       "its %d solve(s) — not paying for another.",
                       challenge.kind, page_flow.SOLVES_PER_PAGE)
        return

    logger.warning("%s detected via %s (sitekey=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be solved "
                       "— continuing with whatever the page already holds.")
        return
    solves[0] += 1
    try:
        token = solve(challenge, args.twocaptcha_key,
                      api_version=args.captcha_api, min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing.",
                     redact_secret_patterns(str(e)))
        return
    await page.evaluate(INJECT_TOKEN_FN, token)
    if state.policy.blocked:
        logger.info("Token injected. Reloading page to continue.")
        await asyncio.sleep(1.5)
        await page.reload({"waitUntil": "domcontentloaded",
                           "timeout": GOTO_TIMEOUT_MS})
    else:
        # See the twin: a reload on a page that was not blocked discards the
        # token, and a form's token is only useful at submit time.
        logger.info("Token injected and left in the page's own response field. "
                    "This page was not blocked, so it is NOT reloaded.")


async def _fetch_safely(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """`_fetch_one_page`, with an unexpected driver exception recorded as a
    failed page instead of ending the run — a target that closes on page 5
    must not throw away pages 1-4. Same rule in every engine."""
    try:
        return await _fetch_one_page(session, args, pool, page_num, url)
    except (ProxyError, KeyboardInterrupt):
        raise
    except Exception as e:  # noqa: BLE001 — recorded, redacted, not swallowed
        logger.error("Page %d raised %s — recording it as a failed page.",
                     page_num, redact_secret_patterns(str(e)))
        outcome = PageOutcome(page_num=page_num, url=url)
        outcome.raised = True
        return outcome


async def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch, classify and parse one page. `page_num` is the SITE page."""
    outcome = PageOutcome(page_num=page_num, url=url)
    block_retries = args.proxy_block_retries if (pool and len(pool) > 1) else 0
    html, state, load_failed = None, None, False
    solves = [0]

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d: %s", page_num, url)
        load_failed, exit_failed, status = False, None, None
        for attempt in range(1, args.retries + 1):
            try:
                response = await session.page.goto(
                    url, {"waitUntil": "domcontentloaded", "timeout": GOTO_TIMEOUT_MS})
                status = response.status if response is not None else None
                load_failed = False
                break
            except (PPTimeout, PageError, NetworkError,
                    asyncio.TimeoutError) as e:
                reason = _proxy_failure(e)
                if reason:
                    exit_failed, load_failed = reason, True
                    break
                load_failed = True
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, e, pause)
                    await asyncio.sleep(pause)

        if exit_failed and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating (%d/%d).",
                           mask(pool.current), exit_failed, block_attempt + 1,
                           block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            await session.relaunch()
            continue
        if exit_failed:
            # Say WHICH failure this was. Chromium reports a dead proxy as a
            # generic error, not a timeout, and the two want opposite
            # responses — "gave up loading" alone sends the reader looking at
            # the site when the exit is what is broken.
            logger.error("The proxy exit %s refused the connection (%s), and "
                         "there is no other exit to try. This is the exit, not "
                         "the site: check the entry, or pass --proxy-file with "
                         "more than one.", mask(pool.current) if pool else "(none)",
                         exit_failed)
        if load_failed:
            break

        await handle_captcha_if_present(session.page, args, solves)
        html = await _content(session.page) or ""
        state = page_flow.classify(html, status_code=status, url=session.page.url,
                                   requested_page=page_num)
        if state.state == page_flow.CONTENT:
            minimum = page_flow.min_matches(MIN_CARD_MATCHES, state.total_results,
                                            page_num)
            rows = await _wait_for_cards(session.page, minimum)
            html = await _content(session.page) or html
            state = page_flow.classify(html, status_code=status,
                                       url=session.page.url,
                                       requested_page=page_num)
            logger.info("Page %d is %s (%d row(s) rendered).", page_num, state, rows)
        else:
            logger.info("Page %d is %s.", page_num, state)

        if state.state == page_flow.UNPAINTED and state.policy.wait_first:
            await _wait_for_cards(session.page, MIN_CARD_MATCHES)
            html = await _content(session.page) or html
            state = page_flow.classify(html, status_code=status,
                                       url=session.page.url,
                                       requested_page=page_num)
            logger.info("After waiting, page %d is %s.", page_num, state)

        if not state.policy.rotate_exit:
            break
        if block_attempt < block_retries:
            logger.warning("Blocked on page %d from %s (%s) — retrying from "
                           "another exit (%d/%d).", page_num, mask(pool.current),
                           state.reason, block_attempt + 1, block_retries)
            pool.advance(f"blocked on page {page_num}")
            await session.relaunch()

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        outcome.proxy_failed = bool(exit_failed)
        return outcome

    outcome.state = state
    outcome.final_url = session.page.url

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    if state.policy.blocked:
        await _dump_debug(session.page, args, page_num, html)
        logger.error("Blocked before parsing on page %d (%s) — exit 3, distinct "
                     "from a listing that genuinely has no rows (exit 4). %s",
                     page_num, state.reason,
                     page_flow.block_advice(state, has_pool=bool(pool)))
        return outcome

    if not state.policy.usable:
        logger.error("Page %d: %s (%s).", page_num, state.reason, url)
        return outcome

    if state.state in (page_flow.EMPTY, page_flow.EXHAUSTED):
        logger.info("Page %d: %s — nothing further to fetch.", page_num, state.reason)
        return outcome

    products = parse_products(html, session.page.url, category=args.category,
                              page=page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)
    short = page_flow.shortfall(state.total_results, page_num, len(products))
    if short:
        logger.warning("%s.", short)
    if not products:
        await _dump_debug(session.page, args, page_num, html)
        # page_flow returns EMPTY or EXHAUSTED for the real end of a listing,
        # and both returned earlier. Getting here means the page HAD a table
        # this code could not read.
        outcome.parse_failed = True
        logger.error("0 rows parsed from a page classified as %s (%s) — "
                     "that is a parse failure, not the end of the listing.",
                     state.state, state.reason)
    outcome.products = products
    return outcome


async def _dump_debug(page, args, page_num: int, html: Optional[str]) -> None:
    debug_html = f"{args.out}_page{page_num}_debug.html"
    with open(debug_html, "w", encoding="utf-8") as f:
        f.write(html or "")
    try:
        await page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                               "fullPage": True})
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not capture screenshot: %s", e)
    logger.warning("Wrote %s (and a .png beside it).", debug_html)


class _BrowserSession:
    """One browser and page, relaunchable on a new exit."""

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.browser = self.page = None

    async def open(self):
        self.browser, self.page = await _open_browser(self.args, self.pool)
        return self

    async def relaunch(self):
        if self.args.cdp_endpoint:
            return   # a remote browser's exit is not ours to change
        try:
            await self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser: %s", e)
        await self.open()

    async def close(self):
        try:
            if self.args.cdp_endpoint:
                await self.page.close()
            else:
                await self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during teardown: %s", e)


def _addressable(first: PageOutcome, second: PageOutcome) -> bool:
    """Did the constructed page-2 URL actually reach page 2? See the twin."""
    if not second.ok or not second.products:
        return False
    first_ids = {p.sku for p in first.products if p.sku}
    return any(p.sku and p.sku not in first_ids for p in second.products)


async def _scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    blocked = False
    stop_reason = "completed"
    total_results = None
    pages_available = None
    addressable = None
    start = args.start_page

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit.")
        pool = None
    # One small request through the exit before a browser is launched, so a
    # rotated password surfaces as a named proxy error.
    check_exit_or_raise(pool, args.url)
    if args.concurrency > 1:
        # Stated rather than silently ignored: this engine drives one browser
        # on one event loop, and its twin is the one with a worker pool.
        logger.warning("--concurrency %d is not implemented in the pyppeteer "
                       "engine — fetching one page at a time. Use "
                       "playwright_scraper.py for concurrent pages.",
                       args.concurrency)

    session = await _BrowserSession(args, pool).open()
    try:
        first = await _fetch_safely(session, args, pool, start, args.url)
        outcomes.append(first)
        if first.state is not None:
            total_results = first.state.total_results
            pages_available = first.state.pages_available
        planned = page_flow.plan_pages(args.pages, start, pages_available)
        if planned < args.pages and first.ok:
            logger.info("The site states %s page(s) for this listing, so this "
                        "run fetches %d rather than the %d asked for.",
                        pages_available, planned, args.pages)

        if not first.ok:
            stop_reason = failure_stop_reason(first)
            blocked = bool(first.state and first.state.policy.blocked)
        elif first.complete_here:
            stop_reason = ("no_results" if first.state.state == page_flow.EMPTY
                           else "start_page_out_of_range")
            if stop_reason == "start_page_out_of_range":
                # Not "the listing is empty": the URL asked for a page past the
                # end, and the site answered with its last page instead. Nothing is
                # concluded about the listing, so this is not a complete run.
                logger.error("--url asks for page %d, but the site states %s "
                             "page(s) for this listing.", start,
                             first.state.pages_available)
        elif planned > 1:
            seen = {p.sku for p in first.products if p.sku}
            previous = first
            for run_page in range(2, planned + 1):
                page_num = page_flow.site_page(start, run_page)
                if pool and pool.rotates_per_page():
                    pool.advance(f"per-page rotation, page {page_num}")
                    await session.relaunch()
                await asyncio.sleep(args.delay)
                outcome = await _fetch_safely(
                    session, args, pool, page_num,
                    page_flow.url_for(args.url, start, run_page))
                outcomes.append(outcome)
                if not outcome.ok:
                    stop_reason = failure_stop_reason(outcome)
                    blocked = bool(outcome.state and outcome.state.policy.blocked)
                    break
                if outcome.complete_here or not outcome.products:
                    stop_reason = "listing_exhausted"
                    break
                if run_page == 2:
                    addressable = _addressable(previous, outcome)
                    if not addressable:
                        # Page 2 stays in the outcomes, as in the twins: it was
                        # fetched and it is what proves the point.
                        logger.error(
                            "Page 2 (%s) returned nothing page 1 did not already "
                            "have, so this listing cannot be paged through by "
                            "URL. Stopping and reporting a PARTIAL run rather "
                            "than a complete-looking one.", outcome.url)
                        stop_reason = "pagination_not_addressable"
                        break
                fresh = [p for p in outcome.products
                         if p.sku is None or p.sku not in seen]
                seen.update(p.sku for p in outcome.products if p.sku)
                if not fresh:
                    logger.info("Page %d added nothing not already seen — "
                                "treating that as the end of the listing.", page_num)
                    stop_reason = "no_new_listings"
                    break
                previous = outcome
        if (stop_reason == "completed"
                and page_flow.reached_last_page(start, planned, pages_available)):
            stop_reason = "listing_exhausted"
    finally:
        await session.close()

    all_products, merge_stats = merge_pages(
        (o.page_num, o.products) for o in outcomes)
    if merge_stats.duplicate_skus_across_pages:
        logger.info("Dropped %d row(s) already seen on an earlier page "
                    "(page(s) %s).", merge_stats.duplicate_skus_across_pages,
                    ", ".join(str(n) for n in merge_stats.duplicate_pages))

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    counted = [o.state.total_results
               for o in sorted(outcomes, key=lambda o: o.page_num)
               if o.state is not None and o.state.total_results is not None]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)
    return finish_run(all_products, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, total_results=total_results,
                      addressable=addressable, merge_stats=merge_stats,
                      pages_missing=[o.page_num for o in outcomes if o.lost],
                      total_results_first=counted[0] if counted else None,
                      total_results_last=counted[-1] if counted else None,
                      pages_available=pages_available, start_page=start,
                      start_url=args.url, final_url=final_url)


def scrape(args) -> int:
    """Synchronous entry point, so every engine here has the same shape.

    `asyncio.run`, not `get_event_loop().run_until_complete`: since Python
    3.12 there is no implicit loop in the main thread, and the older spelling
    dies with "There is no current event loop in thread 'MainThread'" before
    a single page is fetched. Found by RUNNING this engine, not by reading it
    — it imports, answers --help and byte-compiles either way.
    """
    return asyncio.run(_scrape(args))


def build_parser():
    p = argparse.ArgumentParser(description="screener.in scraper (pyppeteer edition)")
    p.add_argument("--url", default=None,
                   help="A screener.in screen (/screens/{id}/{slug}/) or sector "
                        "listing (/market/IN.../). Required unless SCREENER_URL is set.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Defaults to the "
                        "listing's own heading, read from the page.")
    p.add_argument("--pages", type=cli_types.positive_int, default=1,
                   help="Number of pages to fetch (25 rows each).")
    p.add_argument("--delay", type=cli_types.non_negative_float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=cli_types.positive_int, default=1, metavar="N",
                   help="Accepted for parity with the flag contract, but this "
                        "engine fetches one page at a time; N>1 warns and is "
                        "ignored. Use playwright_scraper.py for concurrency.")
    p.add_argument("--retries", type=cli_types.positive_int, default=3,
                   help="Attempts per page load before giving up (default 3)")
    p.add_argument("--retry-delay", type=cli_types.non_negative_float, default=2.0,
                   help="Seconds before the first page-load retry (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="screener_results", help="Output file prefix")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999. Better "
                        "set as SCREENER_PROXY in .env.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default) or per-page, relaunching the browser.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=cli_types.non_negative_int, default=2,
                   help="Retries from OTHER exits when a page comes back blocked.")
    p.add_argument("--twocaptcha-key", default=None,
                   help="2captcha.com API key. Better set as TWOCAPTCHA_KEY in .env.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay when the rows are "
                        "not already readable.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score (0.3, 0.7 or 0.9).")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Not implemented in this engine — pyppeteer cannot set a "
                        "context's locale, timezone and screen the way the "
                        "fingerprint needs. Warns and continues.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the Fingerprint API.")
    p.add_argument("--fp-country", default=None, help="Fingerprint country, ISO alpha-2.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP, e.g. the "
                        "Scraping Browser API endpoint. Better set as "
                        "SCREENER_CDP_ENDPOINT in .env.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success too.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def parse_args(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    env_config.apply(args)
    cli_types.finish_args(p, args, logger)
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint:
        logger.warning("--fingerprint is not implemented in the pyppeteer engine "
                       "— continuing without one. playwright_scraper.py applies "
                       "fingerprints; see fingerprint_client.py.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception:  # noqa: BLE001 — see the twin: a traceback is a log
        import traceback
        logger.error("Crashed:\n%s", redact_secret_patterns(traceback.format_exc()))
        sys.exit(1)
