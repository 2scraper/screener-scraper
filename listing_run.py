"""
listing_run.py
---------------
The page loop shared by the engines that manage no browser: the Scraper API
client and the plain-HTTP engine. It knows how to plan pages, classify and
parse each answer, stop on the data, and hand the result to finish_run —
and nothing about how a page's bytes are obtained.

A transport is a callable `fetch(args, url) -> (html, status, final_url)`.
It does its OWN transient-fault retries (a connection error, a 429 with
Retry-After), because only the transport knows what a retry costs: a new
Scraper API task is billed, a plain GET is not. When it gives up it raises
FetchFailed with a message that is already safe to log.

`refetch_on_policy` is the other half of that split. page_flow's policy says
whether fetching a page AGAIN could change the answer (`policy.retry`); that
is worth doing through the Scraper API, whose every task may leave from a
different exit, and not from one local address, where a bare retry of a
refusal only burns the address further.

The browser engines keep their own loops for now; moving them here is the
coordinator step of the roadmap, and each carries engine-specific behaviour
(rotation, concurrency, painting) that has to be unified first.
"""

import logging
import time
from typing import Callable, List, Optional, Tuple

import page_flow
from output_writer import finish_run, merge_pages
from product_parser import parse_products

_logger = logging.getLogger("listing_run")

Fetch = Callable[[object, str], Tuple[str, Optional[int], Optional[str]]]


class FetchFailed(Exception):
    """The transport gave up on a page. The message is safe to log."""


def fetch_and_parse(args, page_num: int, url: str, fetch: Fetch, *,
                    refetch_on_policy: bool, logger=None):
    """Fetch, classify and parse one page; returns (state, products, final_url).

    `(None, [], None)` when the transport gave up — the caller records that
    as a failed page, never as the end of the listing.
    """
    logger = logger or _logger
    attempts = args.retries + 1 if refetch_on_policy else 1
    for attempt in range(1, attempts + 1):
        try:
            html, status, final_url = fetch(args, url)
        except FetchFailed as e:
            logger.error("%s", e)
            return None, [], None

        if args.dump_html:
            path = args.dump_html if args.pages == 1 else f"{args.dump_html}.page{page_num}"
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info("Raw HTML written to %s", path)

        # The FINAL url, after redirects: a redirect to /register/ is then
        # seen from the address as well as from the page itself.
        state = page_flow.classify(html, status_code=status, url=final_url or url,
                                   requested_page=page_num)
        logger.info("Page %d is %s.", page_num, state)

        if state.policy.retry and attempt < attempts:
            logger.warning("Page %d is %s (%s) on attempt %d — retrying in "
                           "%.1fs.", page_num, state.state, state.reason,
                           attempt, args.retry_delay)
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
            return state, [], final_url

        if not state.policy.usable:
            logger.error("Page %d: %s (%s).", page_num, state.reason, url)
            return state, [], final_url

        if state.state in (page_flow.EMPTY, page_flow.EXHAUSTED):
            return state, [], final_url

        products = parse_products(html, final_url or url, category=args.category,
                                  page=page_num)
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
        return state, products, final_url
    return None, [], None


def page_ok(state, products) -> bool:
    """The page answered the question: usable, not blocked, and not a parse failure."""
    return (state is not None and state.policy.usable
            and not parse_failed(state, products))


def parse_failed(state, products) -> bool:
    """Classified as carrying rows, yet nothing parsed out of it.

    page_flow reports the real end of a listing as EMPTY or EXHAUSTED, both of
    which carry policy.complete. A page that is neither of those, is not a
    challenge, and still yields no rows is a parse or schema failure — and
    treating it as the end of the listing is how a run holding half the
    catalogue reports itself complete.
    """
    return (state is not None and not products and state.policy.usable
            and not state.policy.complete)


def stop_reason_for(state, products, logger=None) -> Optional[str]:
    """Why a fetched page ends the run as a failure, or None if it does not."""
    logger = logger or _logger
    if state is None:
        return "api_error"
    if state.policy.blocked:
        return f"blocked_{state.vendor}"
    if not state.policy.usable:
        return f"page_{state.state}"
    if parse_failed(state, products):
        logger.error("0 rows parsed from a page classified as %s (%s) — that "
                     "is a parse failure, not the end of the listing.",
                     state.state, state.reason)
        # The same two names output_writer.failure_stop_reason gives the
        # browser engines, so a sidecar reads the same whichever wrote it.
        return ("page_never_painted" if state.state == page_flow.UNPAINTED
                else "content_unparsed")
    return None


def run(args, fetch: Fetch, *, engine: str, refetch_on_policy: bool,
        logger=None, before_page: Optional[Callable[[int], None]] = None) -> int:
    """Fetch the listing page by page and write the result; return the exit code.

    `before_page(site_page)` runs before every page after the first — where a
    transport rotates its exit per page.
    """
    logger = logger or _logger
    outcomes: List[Tuple[int, object, list, Optional[str]]] = []
    blocked = False
    stop_reason = "completed"
    total_results = None
    pages_available = None
    addressable = None
    start = args.start_page

    def page(page_num, url):
        state, products, final_url = fetch_and_parse(
            args, page_num, url, fetch, refetch_on_policy=refetch_on_policy,
            logger=logger)
        outcomes.append((page_num, state, products, final_url or url))
        return state, products

    state, products = page(start, args.url)
    if state is not None:
        total_results = state.total_results
        pages_available = state.pages_available
    planned = page_flow.plan_pages(args.pages, start, pages_available)
    if planned < args.pages and state is not None and state.policy.usable:
        logger.info("The site states %s page(s) for this listing, so this run "
                    "fetches %d rather than the %d asked for.",
                    pages_available, planned, args.pages)

    failure = stop_reason_for(state, products, logger)
    if failure:
        stop_reason = failure
        blocked = bool(state and state.policy.blocked)
    elif state.policy.complete:
        stop_reason = ("no_results" if state.state == page_flow.EMPTY
                       else "start_page_out_of_range")
        if stop_reason == "start_page_out_of_range":
            logger.error("--url asks for page %d, but the site states %s "
                         "page(s) for this listing.", start, state.pages_available)
    elif planned > 1:
        seen = {p.sku for p in products if p.sku}
        first_products = products
        for run_page in range(2, planned + 1):
            page_num = page_flow.site_page(start, run_page)
            time.sleep(args.delay)
            if before_page:
                before_page(page_num)
            state, products = page(page_num,
                                   page_flow.url_for(args.url, start, run_page))
            failure = stop_reason_for(state, products, logger)
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
        (page_num, products) for page_num, _state, products, _url in outcomes)
    if merge_stats.duplicate_skus_across_pages:
        logger.info("Dropped %d row(s) already seen on an earlier page "
                    "(page(s) %s).", merge_stats.duplicate_skus_across_pages,
                    ", ".join(str(n) for n in merge_stats.duplicate_pages))

    ok_pages = [o for o in outcomes if page_ok(o[1], o[2])]
    failed = [o[0] for o in outcomes if not page_ok(o[1], o[2])]
    counted = [o[1].total_results for o in sorted(outcomes, key=lambda o: o[0])
               if o[1] is not None and o[1].total_results is not None]
    # The last page that answered, as the browser engines report it — not
    # the start URL, which is already in `start_url`.
    final_url = ok_pages[-1][3] if ok_pages else args.url
    return finish_run(all_products, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed, total_results=total_results,
                      addressable=addressable, merge_stats=merge_stats,
                      total_results_first=counted[0] if counted else None,
                      total_results_last=counted[-1] if counted else None,
                      pages_available=pages_available, start_page=start,
                      start_url=args.url, final_url=final_url,
                      engine=engine)
