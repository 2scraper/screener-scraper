#!/usr/bin/env python3
"""
screener-scraper — Playwright edition (primary engine)
=======================================================

Scrapes screener.in result tables: any stock screen
(`/screens/{id}/{slug}/`) and any sector or industry listing
(`/market/IN08/IN0801/IN080101/`). No screen or sector is hardcoded — what a
run covers is entirely the URL you give it.

Site-specific knowledge in this file is deliberately tiny: the row selector
and row-count floor below, the readiness wait, and this docstring.
Everything about WHAT a row contains lives in product_parser.py; everything
about WHICH ANSWER a page is lives in page_flow.py, shared with the other
engines so they cannot drift.

Four things here are specific to this site and worth knowing, all measured on
2026-09-25:

  * **The table is in the served HTML.** Plain HTTP with no JavaScript gets
    every row, so the readiness wait below resolves on the first poll on a
    healthy page. A browser buys nothing on the data side here; see the
    README's "What the paid products buy you".

  * **The readiness wait never evaluates a string.** screener.in's CSP is
    `script-src 'self' https: 'unsafe-inline' 'wasm-unsafe-eval'` — no
    `unsafe-eval` — so `page.wait_for_function` is refused outright. It took
    this repo's first version down on its first live run. Rows are counted
    through `page.locator(...).count()`, which goes through the protocol.

  * **An out-of-range page answers 200 with the LAST page.** `?page=8` of a
    7-page sector is page 7 again. The site states which page it served
    ("Showing page 7 of 7") and page_flow reads that, and the run plans its
    page count against the site's own "of 7" instead of walking off the end.

  * **A /screens/ URL can come back as the registration page** — seen in
    captures taken through the Scraping Browser API and the Scraper API, and
    not from a residential connection. It is classified as blocked (exit 3)
    with advice, never parsed as an empty screen.

Usage
-----
    python3 playwright_scraper.py \\
        --url "https://www.screener.in/market/IN08/IN0801/IN080101/" \\
        --pages 3 --format both --out it_software

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (not needed with --cdp-endpoint)
"""

import argparse
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

import cli_types
import env_config
import page_flow
from captcha_solver import (INJECT_TOKEN_FN, detect_in_html, detect_in_page,
                            reconcile_detections, solve)
from output_writer import failure_stop_reason, finish_run, merge_pages
from product_parser import SELECTORS, parse_products
from proxy_pool import (ROTATE_MODES, ProxyError, ProxyPool,
                        check_exit_or_raise,
                        from_args as proxy_pool_from_args, mask,
                        redact_secret_patterns, to_playwright)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")

# --- the whole of this engine's site knowledge -----------------------------
ITEM_CARD_SELECTOR = SELECTORS["item_card"]
# How many result rows mean "the table has rendered" on a full page. Lowered
# per page to what the site says the page holds (page_flow.min_matches), so
# the 2-row last page of a listing is not reported as unpainted.
MIN_CARD_MATCHES = 5
# How long to wait for the table. On this site it is in the first response,
# so a healthy page never waits.
RENDER_WAIT_MS = 15000
# The site's own "Next" link is `?page=N+1` relative to the listing — the same
# URL product_parser.page_url builds — so pages are addressed by URL and
# verified against the DATA on page 2 rather than followed link to link.
NEXT_PAGE_SELECTOR = None
# ---------------------------------------------------------------------------


def _chrome_ua(chromium_version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version: that drifts the moment a newer Chromium ships,
    and a UA claiming an older Chrome than the JS engine and TLS handshake
    report is itself a mismatch a fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws:// or http:// URL."""
    return redact_secret_patterns(url)


@dataclass
class PageOutcome:
    """What one page produced. `page_num` is the SITE's page number."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    state: Optional[page_flow.PageState] = None
    load_failed: bool = False
    # Classified as carrying rows and the parser still got nothing out of it:
    # a parser or schema problem — never the end of the listing, which has
    # its own states (EMPTY, EXHAUSTED).
    parse_failed: bool = False
    # Taken off the work queue and never came back: its worker died holding
    # it. Recorded so the page exists in SOME list rather than in none.
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
        """The listing genuinely ended on this page."""
        return self.state is not None and self.state.policy.complete


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",     # nothing listening / refused
    "ERR_TUNNEL_CONNECTION_FAILED",    # CONNECT rejected by the proxy
    "ERR_PROXY_AUTH_UNSUPPORTED",      # auth scheme we cannot satisfy
    "ERR_PROXY_AUTH_REQUESTED",        # credentials missing or wrong
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    A dead proxy and a timeout want opposite responses — a different exit
    versus another try at the same one — and catching only the timeout type
    let this escape as a traceback in a sibling repo.
    """
    text = str(exc)
    return next((m for m in _PROXY_ERROR_MARKERS if m in text), "")


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    A rotation tears this down and calls it again rather than swapping the
    proxy under a live session: cookies issued against exit A, replayed from
    exit B, are a stronger signal than either address alone.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": "en-US"}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch: over --cdp-endpoint the Scraping
        # Browser brings its own fingerprint and stacking a second one on top
        # is a contradiction, not better cover.
        from fingerprint_client import (get_fingerprint, playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key, tags=args.fp_tags,
                             country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        # Must be installed on the context, before any page script runs.
        context.add_init_script(init_script)
    return browser, context, context.new_page()


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    # An explicit timeout, and the connect is WRAPPED: Playwright puts the
    # endpoint — with its password — into the exception message and into a
    # four-line call log underneath it. An exception message is a log.
    try:
        browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
    except Exception as e:  # noqa: BLE001 — re-raised immediately, redacted
        raise RuntimeError(
            f"Could not connect to --cdp-endpoint: "
            f"{redact_secret_patterns(str(e))}") from None
    # Reuse the remote browser's existing context so its fingerprint, session
    # and exit stay intact.
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    # https://2captcha.com/scraper/browser-api/api — a CDP domain that solves
    # captchas inside the browser once it is enabled for the session. Treat
    # Captcha.solveFinished as the success signal and keep this module's own
    # detect+solve as the fallback.
    try:
        cdp = context.new_cdp_session(page)
        cdp.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
        cdp.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] captcha detected."))
        cdp.on("Captcha.waitForSolve", lambda *_: logger.info("[Scraping Browser] captcha sent for solving."))
        cdp.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] captcha solved."))
        cdp.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
    except Exception as e:  # noqa: BLE001 — any non-2Captcha CDP endpoint lands here
        logger.info("Captcha.setAutoSolve is not available on this "
                    "--cdp-endpoint (%s) — relying on this project's own "
                    "detect+solve instead.", redact_secret_patterns(str(e)))
    return browser, context, page


class _BrowserSession:
    """One browser, context and page, relaunchable on a new exit."""

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the real reason
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()   # leave the remote browser running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright raises "Unable to retrieve content because the page is
    navigating" if the document swaps under it — a redirect to /register/
    lands exactly there. Returns None if the page will not hold still, so the
    caller can skip this step instead of failing the run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts.", attempts)
                return None
            page.wait_for_timeout(pause_ms)
    return None


def _wait_for_cards(page, minimum: int, timeout_ms: int = RENDER_WAIT_MS) -> int:
    """Wait for `minimum` result rows, WITHOUT evaluating a string.

    `page.locator(...).count()` goes through the protocol, so it works under
    screener.in's CSP, which has no `unsafe-eval`.

    Returns the number of rows seen. `>=`, not `>`: reaching the minimum IS
    the success case — a sibling repo's `>` reported every page holding
    exactly the floor as unpainted.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    count = 0
    while True:
        try:
            count = page.locator(ITEM_CARD_SELECTOR).count()
        except PWError as e:
            logger.debug("Counting rows failed (%s) — retrying.", e)
            count = 0
        if count >= minimum or time.monotonic() >= deadline:
            return count
        page.wait_for_timeout(400)


def handle_captcha_if_present(page, args, solves: List[int]) -> None:
    """Runs after EVERY navigation, for ANY page — not scoped to one URL.

    Both detectors run and are reconciled; neither short-circuits the other.
    `solves` is this page's one-element spend counter: page_flow.solve_budget
    is the only gate, so the per-page cap is enforced rather than declared.
    """
    html = _content_when_settled(page)
    if html is None:
        return
    challenge = reconcile_detections(
        detect_in_html(html, page.url),
        detect_in_page(lambda js: page.evaluate(js), page_url=page.url))
    if not challenge:
        return

    # Detected is not blocking: a detection on a page whose rows are already
    # here guards nothing. Classifying what is already on the page is instant
    # — no readiness wait — which is why this check can sit here.
    state = page_flow.classify(html, url=page.url)
    if args.solve_captcha == "when-blocked" and not state.policy.may_solve:
        logger.info("%s detected via %s, but the page is %s — not paying to "
                    "solve it. Pass --solve-captcha always to solve anyway.",
                    challenge.kind, challenge.source, state)
        return
    if not page_flow.solve_budget(solves[0]):
        logger.warning("%s detected again, but this page has already spent "
                       "its %d solve(s) — not paying for another.",
                       challenge.kind, page_flow.SOLVES_PER_PAGE)
        return

    logger.warning("%s detected via %s (sitekey=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey)

    # A captcha this run cannot solve must not take the run down with it.
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be solved "
                       "— continuing with whatever the page already holds. The "
                       "run reports exit 3 if it really was blocking.")
        return
    solves[0] += 1
    try:
        token = solve(challenge, args.twocaptcha_key,
                      api_version=args.captcha_api, min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", redact_secret_patterns(str(e)))
        return

    page.evaluate(INJECT_TOKEN_FN, token)
    if state.policy.blocked:
        # The reload is what makes the site hand over the page, because the
        # token travels in the cookie the challenge sets.
        logger.info("Token injected. Reloading page to continue.")
        page.wait_for_timeout(1500)
        page.reload(wait_until="domcontentloaded", timeout=60000)
    else:
        # A form's captcha is read from its field at submit time, so a
        # reload would discard the token — and nothing here submits a form.
        logger.info("Token injected and left in the page's own response field. "
                    "This page was not blocked, so it is NOT reloaded.")


def _fetch_safely(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """`_fetch_one_page`, with an unexpected driver exception recorded as a
    failed page instead of ending the run — a target that closes on page 5
    must not throw away pages 1-4. Same rule in every engine."""
    try:
        return _fetch_one_page(session, args, pool, page_num, url)
    except (ProxyError, KeyboardInterrupt):
        raise
    except Exception as e:  # noqa: BLE001 — recorded, redacted, not swallowed
        logger.error("Page %d raised %s — recording it as a failed page.",
                     page_num, redact_secret_patterns(str(e)))
        outcome = PageOutcome(page_num=page_num, url=url)
        outcome.raised = True
        return outcome


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch, classify and parse one page. `page_num` is the SITE page.

    Never raises for an EXPECTED failure — a timeout, a challenge, a dead exit
    are all recorded on the outcome instead, because what the run should do
    about them differs between the sequential and concurrent paths.

    Always goes through `session.page`, never a captured local: a rotation
    replaces browser, context and page together.
    """
    outcome = PageOutcome(page_num=page_num, url=url)

    # How many times a blocked page may be retried from a DIFFERENT exit. Zero
    # without a pool: there is nowhere else to go, and a bare retry from the
    # same address just burns it further.
    block_retries = args.proxy_block_retries if (pool and len(pool) > 1) else 0
    html, state, load_failed = None, None, False
    solves = [0]

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d: %s", page_num, url)
        load_failed, exit_failed, status = False, None, None
        for attempt in range(1, args.retries + 1):
            try:
                response = session.page.goto(url, wait_until="domcontentloaded",
                                             timeout=60000)
                status = response.status if response is not None else None
                load_failed = False
                break
            except (PWTimeout, PWError) as e:
                reason = _proxy_failure(e)
                if reason:
                    exit_failed, load_failed = reason, True
                    break   # a different exit is the only thing that helps
                load_failed = True
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Timeout loading %s (attempt %d/%d) — retrying "
                                   "in %.1fs.", url, attempt, args.retries, pause)
                    time.sleep(pause)

        if exit_failed and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating (%d/%d).",
                           mask(pool.current), exit_failed, block_attempt + 1,
                           block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if exit_failed:
            logger.error("The proxy exit %s refused the connection (%s), and "
                         "there is no other exit to try. This is the exit, not "
                         "the site: check the entry, or pass --proxy-file with "
                         "more than one.", mask(pool.current) if pool else "(none)",
                         exit_failed)
        if load_failed:
            break

        handle_captcha_if_present(session.page, args, solves)
        html = _content_when_settled(session.page) or ""
        state = page_flow.classify(html, status_code=status, url=session.page.url,
                                   requested_page=page_num)
        if state.state == page_flow.CONTENT:
            minimum = page_flow.min_matches(MIN_CARD_MATCHES, state.total_results,
                                            page_num)
            rows = _wait_for_cards(session.page, minimum)
            html = _content_when_settled(session.page) or html
            state = page_flow.classify(html, status_code=status,
                                       url=session.page.url,
                                       requested_page=page_num)
            logger.info("Page %d is %s (%d row(s) rendered).", page_num, state, rows)
        else:
            logger.info("Page %d is %s.", page_num, state)

        if state.state == page_flow.UNPAINTED and state.policy.wait_first:
            # Served but carrying no table: waiting is what helps, and a
            # reload would throw the wait away. One more look after the wait.
            _wait_for_cards(session.page, MIN_CARD_MATCHES)
            html = _content_when_settled(session.page) or html
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
            session.relaunch()

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        outcome.proxy_failed = bool(exit_failed)
        return outcome

    outcome.state = state
    outcome.final_url = session.page.url

    # Dumping on success too, not only on failure: a run can return the right
    # COUNT with a column silently unpopulated, and then the exact bytes are
    # the only way to tell a parsing bug from a too-early snapshot.
    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    if state.policy.blocked:
        _dump_debug(session.page, args, page_num, html)
        logger.error("Blocked before parsing on page %d (%s) — this is exit 3, "
                     "distinct from a listing that genuinely has no rows "
                     "(exit 4). %s", page_num, state.reason,
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
        _dump_debug(session.page, args, page_num, html)
        # Not "the listing ended here": page_flow reports the end of a
        # listing as EMPTY or EXHAUSTED and both returned above. Reaching
        # this line means the page HAD a table and this code could not read
        # it — treating it as the end is how a schema change becomes a
        # successful-looking run holding half the listing.
        outcome.parse_failed = True
        logger.error("0 rows parsed from a page classified as %s (%s) — "
                     "that is a parse failure, not the end of the listing. "
                     "Saved what the browser actually saw.",
                     state.state, state.reason)
    outcome.products = products
    return outcome


def _dump_debug(page, args, page_num: int, html: Optional[str]) -> None:
    debug_html = f"{args.out}_page{page_num}_debug.html"
    with open(debug_html, "w", encoding="utf-8") as f:
        f.write(html or "")
    try:
        page.screenshot(path=f"{args.out}_page{page_num}_debug.png", full_page=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not capture screenshot: %s", e)
    logger.warning("Wrote %s (and a .png beside it).", debug_html)


def _worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to a
    different offset, so workers start on distinct exits and no thread needs a
    lock: the concurrency is safe by construction rather than by discipline.
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def _fetch_pages_concurrently(args, pool, specs, concurrency: int):
    """Fetch `specs` [(page_num, url), ...] across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it.
    """
    work = queue.Queue()
    for spec in specs:
        work.put(spec)

    results = []
    results_lock = threading.Lock()
    # The page numbers that came back past the end. Which one was LOWEST
    # decides whether the pages still in the queue were skipped legitimately.
    empty_pages = []
    # Set when a page comes back past the end of the listing, so asking for
    # 50 pages of a 5-page listing costs at most N-1 extra fetches.
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                session = _BrowserSession(pw, args, _worker_pool(pool, index)).open()
                try:
                    first = True
                    while not exhausted.is_set():
                        try:
                            page_num, url = work.get_nowait()
                        except queue.Empty:
                            break
                        if not first:
                            time.sleep(args.delay)
                        first = False
                        try:
                            outcome = _fetch_one_page(session, args, session.pool,
                                                      page_num, url)
                        except Exception:  # noqa: BLE001
                            # get_nowait already removed this page from the
                            # queue; letting the exception out would leave it
                            # in no list at all.
                            import traceback
                            logger.error(
                                "[%s] page %d raised — recording it as a failed "
                                "page and retiring this worker.\n%s", name, page_num,
                                redact_secret_patterns(traceback.format_exc()))
                            lost = PageOutcome(page_num=page_num, url=url)
                            lost.lost = True
                            with results_lock:
                                results.append(lost)
                            break
                        with results_lock:
                            results.append(outcome)
                        if outcome.ok and (outcome.complete_here
                                           or not outcome.products):
                            logger.info("[%s] page %d is past the end of the "
                                        "listing — stopping dispatch.",
                                        name, page_num)
                            with results_lock:
                                empty_pages.append(page_num)
                            exhausted.set()
                finally:
                    session.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            import traceback
            logger.error("[%s] died; any page it was holding is reconciled "
                         "into the results below, and whatever is still "
                         "queued is reported unattempted.\n%s", name,
                         redact_secret_patterns(traceback.format_exc()))

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Anything still queued was never attempted. Not reported as failed.
    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait()[0])
        except queue.Empty:
            break

    # Every page was asked for, so every page must be in exactly one list.
    by_page = dict(specs)
    accounted = {o.page_num for o in results} | set(unattempted)
    for page_num in sorted(set(by_page) - accounted):
        logger.error("Page %d left the queue and never came back — recording it "
                     "as a failed page.", page_num)
        lost = PageOutcome(page_num=page_num, url=by_page[page_num])
        lost.lost = True
        results.append(lost)

    return (results, sorted(unattempted), exhausted.is_set(),
            min(empty_pages) if empty_pages else None)


def _addressable(first: PageOutcome, second: PageOutcome) -> bool:
    """Did the constructed page-2 URL actually reach page 2?

    Checked against the DATA, which is what the family's most expensive bug
    calls for: if page 2 carries nothing page 1 did not already have, the
    page parameter did not work and pages 3..N cannot be addressed either.
    """
    if not second.ok or not second.products:
        return False
    first_ids = {p.sku for p in first.products if p.sku}
    return any(p.sku and p.sku not in first_ids for p in second.products)


def _between_pages(session, args, pool, page_num: int) -> None:
    """The pause, and the per-page rotation, before every page after the first.

    One helper so page 2 gets the same treatment as pages 3..N in every
    engine — the sibling this was ported from paused before page 2 in two
    engines and not in the third.
    """
    if pool and pool.rotates_per_page():
        pool.advance(f"per-page rotation, page {page_num}")
        session.relaunch()
    time.sleep(args.delay)


def scrape(args) -> int:
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
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    # One small request through the exit before a browser is launched, so a
    # rotated password surfaces as a named proxy error rather than as three
    # 60s navigation timeouts.
    check_exit_or_raise(pool, args.url)

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        if args.cdp_endpoint:
            logger.warning("--concurrency is ignored with --cdp-endpoint: the "
                           "Scraping Browser API allows one live connection per "
                           "profile, so workers collide (profile_locked). Use "
                           "several pids, one run each.")
            concurrency = 1
        elif not pool:
            logger.warning("--concurrency %d with no proxy pool: every worker "
                           "leaves from the SAME address, which is a faster way "
                           "to get that address scored than to gather data. Pass "
                           "--proxy-file to spread the load.", concurrency)
        if pool and pool.rotates_per_page():
            logger.info("--proxy-rotate per-page is redundant under "
                        "--concurrency: each worker already holds its own exit.")
        if concurrency > 8:
            logger.warning("--concurrency %d means %d browsers at once "
                           "(~150-300MB each).", concurrency, concurrency)

    session = None
    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            # Page 1 alone: its content decides whether the rest exist at all.
            first = _fetch_safely(session, args, pool, start, args.url)
            outcomes.append(first)
            if first.state is not None:
                total_results = first.state.total_results
                pages_available = first.state.pages_available
            planned = page_flow.plan_pages(args.pages, start, pages_available)
            if planned < args.pages and first.ok:
                logger.info("The site states %s page(s) for this listing, so "
                            "this run fetches %d rather than the %d asked for.",
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
                # Page 2 is fetched sequentially whatever --concurrency says:
                # it is what proves pages can be addressed by URL at all.
                _between_pages(session, args, pool, 2)
                second = _fetch_safely(session, args, pool,
                                         page_flow.site_page(start, 2),
                                         page_flow.url_for(args.url, start, 2))
                outcomes.append(second)
                # Decided only by a page 2 that answered: a blocked or failed
                # page 2 says nothing about the ?page= convention, and the
                # twins leave it null in that case too.
                if second.ok and not second.complete_here:
                    addressable = _addressable(first, second)

                if not second.ok:
                    stop_reason = failure_stop_reason(second)
                    blocked = bool(second.state and second.state.policy.blocked)
                elif second.complete_here or not second.products:
                    stop_reason = "listing_exhausted"
                elif not addressable:
                    logger.error(
                        "Page 2 (%s) returned nothing that page 1 did not "
                        "already have, so this listing cannot be paged through "
                        "by URL. Stopping and reporting a PARTIAL run rather "
                        "than a complete-looking one.", second.url)
                    stop_reason = "pagination_not_addressable"
                elif planned > 2:
                    specs = [(page_flow.site_page(start, n),
                              page_flow.url_for(args.url, start, n))
                             for n in range(3, planned + 1)]
                    if concurrency > 1:
                        session.close()
                        session = None
                        logger.info("Fetching pages %d-%d across %d workers%s.",
                                    specs[0][0], specs[-1][0], concurrency,
                                    f" over {len(pool)} exit(s)" if pool else "")
                        more, unattempted, ran_out, empty_at = (
                            _fetch_pages_concurrently(args, pool, specs,
                                                      concurrency))
                        outcomes.extend(more)
                        failed = [o for o in more if not o.ok]
                        if failed:
                            worst = min(failed, key=lambda o: o.page_num)
                            stop_reason = failure_stop_reason(worst)
                            blocked = any(o.state and o.state.policy.blocked for o in more)
                        elif ran_out:
                            # Workers do not finish in page order: pages
                            # numbered BELOW the one that ran out may still
                            # have been queued, and those hold rows.
                            gap = [n for n in unattempted
                                   if empty_at is None or n < empty_at]
                            if gap:
                                logger.error(
                                    "Page %s was past the end and stopped "
                                    "dispatch, but page(s) %s were never "
                                    "fetched and come BEFORE it — this is a "
                                    "gap, not the end of the listing.",
                                    empty_at, ", ".join(str(n) for n in gap))
                                stop_reason = "pages_unattempted"
                            else:
                                stop_reason = "listing_exhausted"
                        elif unattempted:
                            stop_reason = "pages_unattempted"
                    else:
                        seen = {p.sku for o in outcomes for p in o.products if p.sku}
                        for page_num, url in specs:
                            _between_pages(session, args, pool, page_num)
                            outcome = _fetch_safely(session, args, pool, page_num, url)
                            outcomes.append(outcome)
                            if not outcome.ok:
                                stop_reason = failure_stop_reason(outcome)
                                blocked = bool(outcome.state and outcome.state.policy.blocked)
                                break
                            if outcome.complete_here or not outcome.products:
                                stop_reason = "listing_exhausted"
                                break
                            fresh = [p for p in outcome.products
                                     if p.sku is None or p.sku not in seen]
                            seen.update(p.sku for p in outcome.products if p.sku)
                            # A page that contributes nothing new means the end
                            # of the listing — or pagination looping back on
                            # itself. Either way this is a property of the DATA.
                            if not fresh:
                                logger.info("Page %d added nothing not already "
                                            "seen — treating that as the end of "
                                            "the listing.", page_num)
                                stop_reason = "no_new_listings"
                                break
            if (stop_reason == "completed"
                    and page_flow.reached_last_page(start, planned, pages_available)):
                # Every page the site has was fetched: that is the end of
                # the listing, not merely the end of --pages — and the
                # difference is what `coverage` tells diff_runs.py.
                stop_reason = "listing_exhausted"
        finally:
            if session is not None:
                session.close()

    return _finish(args, outcomes, blocked, stop_reason, total_results,
                   pages_available, addressable)


def _finish(args, outcomes, blocked, stop_reason, total_results,
            pages_available, addressable) -> int:
    # Merge once, in PAGE order — not in the order pages happened to finish.
    all_products, merge_stats = merge_pages(
        (o.page_num, o.products) for o in outcomes)
    if merge_stats.duplicate_skus_across_pages:
        logger.info("Dropped %d row(s) already seen on an earlier page "
                    "(page(s) %s). Recorded in the metadata: a repeat means "
                    "the listing shifted under the run.",
                    merge_stats.duplicate_skus_across_pages,
                    ", ".join(str(n) for n in merge_stats.duplicate_pages))

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    missing_pages = [o.page_num for o in outcomes if o.lost]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)
    # The site's own match count, as of the run's first page and its last. A
    # change between them says the listing was edited mid-run — the one
    # signal that also catches a DELETION, which leaves no duplicate behind.
    counted = [o.state.total_results for o in sorted(outcomes, key=lambda o: o.page_num)
               if o.state is not None and o.state.total_results is not None]

    return finish_run(all_products, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, total_results=total_results,
                      addressable=addressable, pages_missing=missing_pages,
                      merge_stats=merge_stats,
                      total_results_first=counted[0] if counted else None,
                      total_results_last=counted[-1] if counted else None,
                      pages_available=pages_available, start_page=args.start_page,
                      start_url=args.url, final_url=final_url)


def build_parser():
    p = argparse.ArgumentParser(description="screener.in scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="A screener.in screen (/screens/{id}/{slug}/) or sector "
                        "listing (/market/IN08/IN0801/IN080101/). Required unless "
                        "SCREENER_URL is set in the environment or in .env.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Defaults to the "
                        "listing's own heading, read from the page.")
    p.add_argument("--pages", type=cli_types.positive_int, default=1,
                   help="Number of pages to fetch (25 rows each). Never more "
                        "than the site says the listing has.")
    p.add_argument("--delay", type=cli_types.non_negative_float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=cli_types.positive_int, default=1, metavar="N",
                   help="Fetch pages 3..N through N parallel workers (default 1). "
                        "Each worker runs its own browser and holds its own proxy "
                        "exit. Pages 1 and 2 are always fetched alone — page 2 is "
                        "what proves the listing can be paged through by URL. "
                        "Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=cli_types.positive_int, default=3,
                   help="Attempts per page load before giving up (default 3); the "
                        "pause between attempts doubles each time.")
    p.add_argument("--retry-delay", type=cli_types.non_negative_float, default=2.0,
                   help="Seconds before the first page-load retry (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="screener_results", help="Output file prefix")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy). Better set as SCREENER_PROXY in .env "
                        "— a credential on the command line lands in shell history.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. Wins "
                        "over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. per-page: "
                        "a new exit per page, relaunching the browser each time so "
                        "the session does not follow the IP around.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do not all "
                        "begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=cli_types.non_negative_int, default=2,
                   help="When a page comes back blocked, retry it from this many "
                        "OTHER exits before giving up (default 2).")
    p.add_argument("--twocaptcha-key", default=None,
                   help="2captcha.com API key. Better set as TWOCAPTCHA_KEY in .env.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current JSON "
                        "API (api.2captcha.com/createTask); v1 is the legacy "
                        "in.php/res.php pair.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a challenge if "
                        "the rows are not already readable. always: solve whenever "
                        "one is detected. No captcha was configured anywhere on "
                        "screener.in when this was written.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or 0.9). "
                        "Ignored for Turnstile and v2 widgets.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off by "
                        "default so a failed run cannot overwrite a good result "
                        "with an empty one.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a fingerprint from 2captcha's Fingerprint API and "
                        "apply it to the launched browser. Needs --twocaptcha-key. "
                        "Ignored with --cdp-endpoint.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the Fingerprint API (default: "
                        "Windows). A list is rejected with HTTP 400.")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to your "
                        "proxy's exit country — a US fingerprint on an Indian IP "
                        "is a contradiction.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP instead of "
                        "launching Playwright's Chromium, e.g. the Scraping Browser "
                        "API endpoint. Better set as SCREENER_CDP_ENDPOINT in .env. "
                        "--proxy, --fingerprint and --headless/--headful are "
                        "ignored when this is set.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success as "
                        "well as failure.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def parse_args(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)
    cli_types.finish_args(p, args, logger)
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it is a separate subscription).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the Scraping "
                       "Browser supplies its own.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception:  # noqa: BLE001 — a traceback is a log, and it is printed
        # A crash still reports exit 1 and still says where it happened; what
        # it must never do is print a credential on the way out.
        import traceback
        logger.error("Crashed:\n%s", redact_secret_patterns(traceback.format_exc()))
        sys.exit(1)
