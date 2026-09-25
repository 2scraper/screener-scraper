"""
page_flow.py
-------------
What kind of page came back, and what a run should do about it.

screener.in answers a listing request in six distinguishable ways, and five
of them want a different response — which is the test this family's template
sets for adding this module rather than writing the triage three times, once
per engine, and letting the three drift:

    content      the results table is there with rows in it           parse
    empty        the site says "0 results found" itself               stop, exit 4
    exhausted    the site says it served a DIFFERENT page than the
                 one asked for — ?page=8 of a 7-page listing is
                 answered with page 7                                  stop, complete
    unpainted    built out of the site's own assets, no table yet     wait, then retry
    not_found    the site's own 404 page — the URL is wrong           stop, say so
    server_error an HTTP 5xx other than 503 — the site failing, not
                 refusing                                             stop, exit 5
    blocked      the registration wall, a refusal status, a
                 challenge, or a page not built by the site at all    rotate, exit 3

The decision is DATA (`STATE_POLICY`), not three copies of an if-chain, so an
engine cannot quietly disagree with its twins about whether a page is worth
retrying or worth paying for.

**Signal order is by how much a signal proves, not by how cheap it is.** The
site's own statement about the page — "164 results found: Showing page 7 of
7" — outranks every heuristic, so a real page cannot be reported as blocked
because it happened to reference few assets, and a real page past the end
cannot be parsed as fresh rows. The asset count only ever runs on a response
that has already failed to produce any listing data.

No JavaScript crosses this module's boundary: the engines pass HTML, a
status where their driver has one, and the URL the browser ended up on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from product_parser import (MIN_ASSET_REFERENCES, PAGE_SIZE,
                            asset_reference_count, count_cards,
                            detect_bot_challenge, is_login_wall, page_info,
                            page_url, requested_page_number)

CONTENT = "content"
EMPTY = "empty"
EXHAUSTED = "exhausted"
UNPAINTED = "unpainted"
NOT_FOUND = "not_found"
SERVER_ERROR = "server_error"
BLOCKED = "blocked"

# HTTP statuses that are a refusal rather than a page. screener.in answered
# every request measured on 2026-09-25 with 200 — 15 back-to-back requests
# with no delay included, so no rate limit was reached — and a missing page
# with 404. Anything in here is the primary blocked signal where a status is
# available at all; Selenium never has one.
REFUSAL_STATUSES = (401, 403, 405, 406, 429, 503)

# The site's own 404 page: "Error 404: Page Not Found - Screener", measured on
# a non-existent screen, a non-existent sector and /screens/raw/?query=...
_NOT_FOUND_TITLE = "Error 404: Page Not Found"


def expected_records(total_results, site_page: int, page_size: int = PAGE_SIZE):
    """How many rows page `site_page` should hold, or None if unknowable.

    The site's arithmetic is exact here — page 220 of the 5,477-company "All
    Stocks" screen held exactly 2 rows (5477 - 219 x 25) — so a short page is
    measurable rather than guessed.
    """
    if not isinstance(total_results, int) or total_results <= 0:
        return None
    remaining = total_results - page_size * (site_page - 1)
    if remaining <= 0:
        return 0
    return min(page_size, remaining)


def shortfall(total_results, site_page: int, got: int,
              page_size: int = PAGE_SIZE) -> Optional[str]:
    """A message naming a page that came back short, or None."""
    expected = expected_records(total_results, site_page, page_size)
    if expected is None or got >= expected:
        return None
    return (f"page {site_page} returned {got} row(s) where the site's own "
            f"count of {total_results} result(s) implies {expected}")


def min_matches(floor: int, total_results=None, site_page: int = 1) -> int:
    """How many rows mean "this table has painted" for THIS page.

    The engine's floor, lowered to what the page can actually hold: the last
    page of "All Stocks" holds 2 rows and a 16-company screen holds 16, and a
    wait for more than the page holds would time out on a table that is fully
    there. The family's "never wait for one match" rule is about a GENERIC
    link pattern resolving on an unrelated link; the selector here is the
    site's own row attribute, which nothing else on the page carries, so a
    one-row page may wait for its one row.
    """
    expected = expected_records(total_results, site_page)
    if expected is None or expected <= 0:
        return floor
    return max(1, min(floor, expected))


@dataclass(frozen=True)
class PagePolicy:
    """What to do about a page in one state."""
    retry: bool           # fetching it again could plausibly change the answer
    wait_first: bool      # give the page time to paint before retrying
    rotate_exit: bool     # a different proxy exit is what might help
    may_solve: bool       # a captcha here is worth paying to solve
    blocked: bool         # counts as exit 3 if the run ends with nothing
    complete: bool        # the listing genuinely ended here
    usable: bool          # the page answers the question the run asked


# Consulted by every engine — a constant nothing reads is the same defect as
# dead code, so the engines take their retry budget, their rotation decision
# and their solve decision from HERE and nowhere else.
STATE_POLICY = {
    CONTENT:   PagePolicy(retry=False, wait_first=False, rotate_exit=False,
                          may_solve=False, blocked=False, complete=False,
                          usable=True),
    EMPTY:     PagePolicy(retry=False, wait_first=False, rotate_exit=False,
                          may_solve=False, blocked=False, complete=True,
                          usable=True),
    EXHAUSTED: PagePolicy(retry=False, wait_first=False, rotate_exit=False,
                          may_solve=False, blocked=False, complete=True,
                          usable=True),
    # Served but not painted is a WAIT, not a refetch: refetching a shell
    # just buys another shell. On this site it should not happen at all —
    # the table is in the first response — so reaching it is worth a log.
    UNPAINTED: PagePolicy(retry=True, wait_first=True, rotate_exit=False,
                          may_solve=False, blocked=False, complete=False,
                          usable=True),
    # The URL is wrong. No retry, no rotation and no solve can change that,
    # and it is not "blocked" either — calling it that sends a reader to buy
    # a proxy for a typo.
    NOT_FOUND: PagePolicy(retry=False, wait_first=False, rotate_exit=False,
                          may_solve=False, blocked=False, complete=False,
                          usable=False),
    # The site failing rather than refusing: not exit 3, and not worth a
    # different exit or a solve. Never measured on screener.in — 0 of the
    # requests made while writing this answered 5xx — so it is kept narrow.
    SERVER_ERROR: PagePolicy(retry=False, wait_first=False, rotate_exit=False,
                             may_solve=False, blocked=False, complete=False,
                             usable=False),
    BLOCKED:   PagePolicy(retry=True, wait_first=False, rotate_exit=True,
                          may_solve=True, blocked=True, complete=False,
                          usable=False),
}

# How many solves one page may buy. Every engine calls the captcha handler
# once per attempt, and a cap that only one of two call sites counted let a
# sibling repo's single page buy three solves; `solve_budget` is the one
# gate both call sites go through, and the suite counts them.
SOLVES_PER_PAGE = 1


def solve_budget(spent: int) -> bool:
    """Whether another solve on this page is within SOLVES_PER_PAGE."""
    return spent < SOLVES_PER_PAGE


@dataclass
class PageState:
    """The classification of one response, with the evidence behind it."""
    state: str
    reason: str
    vendor: Optional[str] = None
    total_results: Optional[int] = None
    served_page: Optional[int] = None
    pages_available: Optional[int] = None
    card_count: int = 0
    status_code: Optional[int] = None

    @property
    def policy(self) -> PagePolicy:
        return STATE_POLICY[self.state]

    def __str__(self) -> str:
        return f"{self.state} ({self.reason})"


def classify(html: Optional[str], status_code: Optional[int] = None,
             url: Optional[str] = None,
             requested_page: Optional[int] = None) -> PageState:
    """Classify one response.

    `status_code` is optional — Selenium never has one. `url` is where the
    browser ENDED UP, which is how a redirect to /register/ is seen.
    `requested_page` is the site page the fetch asked for; when omitted it is
    read from `url`, which is only right when nothing redirected.

    Ordered by how much each signal proves:

    1. The site's own page info. "0 results found" answers "empty" outright;
       a served page number different from the one asked for answers "past
       the end" outright — `?page=8` of 7 is page 7 again, full of real rows,
       and nothing else on the page says so.
    2. Rows carrying the site's own data-row-company-id.
    3. The site's own 404 page.
    4. A refusal status.
    5. The registration wall — an HTTP 200 page, so after the status.
    6. A challenge's own loader, and only now: a marker scan on a page that
       HAS rows could only ever produce a false positive.
    7. Whether the response is built out of the site's own assets at all.
       This is the one that classifies Chromium's network-error page
       correctly, since that page carries the site's hostname in its title.
    """
    html = html or ""
    total, served, available = page_info(html)
    cards = count_cards(html)
    asked = requested_page if requested_page is not None else requested_page_number(url or "")
    common = dict(total_results=total, served_page=served,
                  pages_available=available, card_count=cards,
                  status_code=status_code)

    if total == 0:
        return PageState(EMPTY, "the site reports 0 results for this listing",
                         **common)
    if served is not None and served != asked:
        return PageState(EXHAUSTED,
                         f"asked for page {asked}, the site served page "
                         f"{served} of {available} — past the end of the "
                         f"listing", **common)
    if cards:
        return PageState(CONTENT, f"{cards} result row(s)", **common)
    if total:
        # The site states a non-zero count for the page it says it served,
        # and no row is here. That is not the end of the listing and not a
        # block: the page was served and this code could not see its table.
        return PageState(CONTENT,
                         f"the site states {total} result(s) but no result "
                         f"row was found", **common)

    if status_code == 404 or _NOT_FOUND_TITLE in html:
        return PageState(NOT_FOUND, "the site's own 404 page — check the URL",
                         vendor="not_found", **common)

    if (isinstance(status_code, int) and status_code >= 500
            and status_code not in REFUSAL_STATUSES):
        return PageState(SERVER_ERROR, f"HTTP {status_code} — the site failed "
                         f"to serve the page", vendor="server_error", **common)

    if status_code in REFUSAL_STATUSES:
        return PageState(BLOCKED, f"HTTP {status_code}", vendor="http", **common)

    if is_login_wall(html, url):
        return PageState(BLOCKED, "redirected to the registration/login page",
                         vendor="login_wall", **common)

    vendor = detect_bot_challenge(html)
    if vendor:
        return PageState(BLOCKED, f"a {vendor} challenge page", vendor=vendor,
                         **common)

    assets = asset_reference_count(html)
    if assets < MIN_ASSET_REFERENCES:
        return PageState(
            BLOCKED,
            f"the response references screener.in's own asset host {assets} "
            f"time(s) (every served page references it 25+ times), so it was "
            f"not built by the site — an interstitial or the browser's own "
            f"error page", vendor="unknown", **common)

    return PageState(UNPAINTED,
                     "built out of the site's own assets but carrying no "
                     "results table yet", **common)


def block_advice(state: PageState, has_pool: bool) -> str:
    """What a reader should actually DO about this block.

    Site-specific on purpose: a generic "try a proxy" wastes an afternoon
    where it is wrong.
    """
    vendor = state.vendor or "unknown"
    if vendor == "login_wall":
        return (
            "screener.in answered with its registration page instead of the "
            "listing. That is not a captcha — there is no widget on it for "
            "any solver — and it is not a refusal status either. The "
            "/screens/ pages this repo was tested on were served in full to "
            "plain HTTP from a residential connection on 2026-09-25, while "
            "captures taken through the Scraping Browser API (2026-08-24) "
            "and the Scraper API (2026-08-23) landed here. So the exit is "
            "the likely variable: try without --cdp-endpoint from an "
            "ordinary connection first, "
            + ("then --proxy-rotate across other exits." if has_pool
               else "then --proxy with a residential Indian exit.")
            + " /market/ sector pages were not redirected in any capture.")
    if vendor == "http":
        return (f"screener.in answered HTTP {state.status_code}. It answered "
                f"200 to every request measured, including 15 with no delay "
                f"between them, so this is new: raise --delay first, then "
                + ("rotate with --proxy-rotate." if has_pool
                   else "try --proxy."))
    if vendor == "unknown":
        return ("the response was not built by screener.in at all — usually "
                "the browser's own network-error page, which means the exit "
                "(proxy, DNS, network) failed rather than the site. Check "
                "the proxy before anything else.")
    return (f"a {vendor} challenge. None was configured anywhere on "
            f"screener.in when this repo was written, so this is new: rerun "
            f"with --dump-html and look at what the page carries.")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def site_page(start_page: int, run_page: int) -> int:
    """The site page that the run's `run_page`-th fetch asks for.

    A run started on `?page=3` fetches site pages 3, 4, 5..., so the run's
    counter and the site's number differ by the start. Every URL, every
    `page` column and every served-page comparison uses the SITE number.
    """
    return start_page + run_page - 1


def url_for(start_url: str, start_page: int, run_page: int) -> str:
    """The URL for the run's `run_page`-th fetch."""
    if run_page == 1:
        return start_url
    return page_url(start_url, site_page(start_page, run_page))


def plan_pages(requested: int, start_page: int,
               pages_available: Optional[int]) -> int:
    """How many pages this run should fetch, given what the site states.

    Where the site states its page count, the run PLANS against it rather
    than walking off the end: asking for 50 pages of a 7-page listing costs 7
    fetches, not 7 plus 43 copies of page 7.
    """
    if not isinstance(pages_available, int) or pages_available < 1:
        return requested
    return max(1, min(requested, pages_available - start_page + 1))


def reached_last_page(start_page: int, fetched: int,
                      pages_available: Optional[int]) -> bool:
    """Whether a run that fetched `fetched` pages from `start_page` reached
    the last page the site says the listing has."""
    return (isinstance(pages_available, int)
            and start_page + fetched - 1 >= pages_available)
