"""
product_parser.py
------------------
Extraction for screener.in listing pages. **This module IS the site** — every
other module in this repo is family core with a handful of named constants.

What the captures actually say — answered from the dumps, not from
expectation. Sources: plain-HTTP captures taken 2026-09-25 from a residential
connection (a sector page and its page 2, three `/screens/` pages of very
different size, page 220 of 220, out-of-range and malformed `?page=` values,
the registration and login pages, the site's 404) plus two captures taken on
2026-08-23/24 through the Scraping Browser API and the Scraper API, both of
which landed on `/register/`.

1. **There is no JSON-LD and no embedded JSON.** 0 `application/ld+json`
   blocks on every capture, and no `__NEXT_DATA__`-style state object: the
   results table is plain server-rendered HTML, complete in the first
   response with no JavaScript executed. The family's usual structured-data
   primary path therefore does not exist here.

2. **The primary path is the site's own data attribute.** Every result row
   is `<tr data-row-company-id="3365">` — the site's internal company id,
   the same anchor the family prefers over any class when a site publishes
   no structured data. Measured: 108 distinct ids across the captures, each
   mapping to exactly one `/company/...` URL and each URL to exactly one id.
   Rows WITHOUT the attribute are the header row, which the site repeats
   part-way down the table (2 header rows on every capture). Class names are
   never used: `data-table text-nowrap striped ...` is styling.

3. **The columns are the page's own, and they differ between pages.** A
   sector page and "All Stocks" render 11 columns; "Magic Formula" renders
   14 (adding ROIC, Earnings Yield and Book Value). A column is identified by
   its header's `data-tooltip` — the site's own full name for the metric
   ("Market Capitalization"), which is also the sort key it builds its own
   `?sort=market+capitalization` links from — and never by its POSITION,
   which moves the moment a screen adds a column. The unit is the header's
   `<span>` ("Rs.", "Rs.Cr.", "%", or empty for P/E).

4. **The site states which page it served, and an out-of-range page lies.**
   `<div data-page-info>164 results found: Showing page 1 of 7</div>`.
   `?page=8`, `?page=999` and `?page=0` of that 7-page listing all answer
   HTTP 200 with page SEVEN's 14 real rows — the same rows, no redirect, no
   error. `?page=abc` answers with page 1. A run that trusted its own
   request would re-collect the last page for as long as it was asked to and
   report success; `served_page()` is what settles it.

5. **`?limit=` is ignored for an anonymous visitor.** The page offers
   "Results per page 10 / 25 / 50", and `?limit=10` and `?limit=50` both
   still return 25 rows and "page 1 of 7" (measured 2026-09-25). This repo
   therefore has no `--limit` flag: a flag that changes nothing is a setting
   that looks configurable and is not.

6. **The rank is global.** Page 2 starts at "26.", page 220 of All Stocks
   ends at "5477." against the site's own "5477 results found" — so a gap
   in a merged run is arithmetic, not a threshold (see `rank_gaps`).

7. **Nothing on a listing page is a captcha.** 0 occurrences of any vendor's
   loader, widget or challenge vocabulary on every plain-HTTP capture. The
   Scraping Browser capture carries 16 `chrome-extension://` hunter scripts
   and an empty `<captcha-widgets>` mount — the auto-solve extension's own
   injection, not the site's (see BOT_CHALLENGE_MARKERS).
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from output_writer import Product

logger = logging.getLogger("product_parser")

SITE_HOST = "www.screener.in"

SELECTORS = {
    # The site's own data attribute on each result row. Also the engines'
    # "has the table rendered?" marker. The header rows carry no such
    # attribute, so they can never satisfy a readiness wait.
    "item_card": "tr[data-row-company-id]",
}

# A company's own URL: /company/<TICKER-OR-BSE-CODE>/ with an optional
# /consolidated/ — 71 of 108 measured rows carried it, 37 did not. It is how
# the company link in a row is told apart from the header's sort links.
_COMPANY_PATH_RE = re.compile(r"^/company/([A-Za-z0-9&.\-]+)/(consolidated/)?$")
_ROW_ID_RE = re.compile(r'data-row-company-id="(\d+)"')

# "164 results found: Showing page 1 of 7". Whitespace-tolerant, because the
# site pretty-prints its templates and a browser re-serialises them.
_PAGE_INFO_RE = re.compile(
    r"([\d,]+)\s+results?\s+found\s*:?\s*Showing\s+page\s+(\d+)\s+of\s+(\d+)",
    re.IGNORECASE)
_TOTAL_ONLY_RE = re.compile(r"([\d,]+)\s+results?\s+found", re.IGNORECASE)

# What a full page of this site holds for an anonymous visitor. 25 on every
# capture of every page kind, and ?limit= does not change it (see point 5).
PAGE_SIZE = 25

# ---------------------------------------------------------------------------
# Blocked-page detection
# ---------------------------------------------------------------------------
# Counted on every capture before anything went in (the family's rule: a
# marker on a page you know is good is not a marker). screener.in is served
# by nginx with no bot manager in front of it, and no captcha is configured
# anywhere on it — 0 of 11 plain-HTTP captures carry any vendor string.
#
# The set is therefore the family's candidates reduced to LOADER PATHS and
# challenge BOOTSTRAP vocabulary, because the Scraping Browser capture shows
# what a looser set would do: it carries "captcha" 21 times, "turnstile" 3
# times and "recaptcha" twice, all inside chrome-extension:// hunter scripts
# on a page with no challenge on it. Every entry below scores ZERO on that
# capture WITHOUT the extension strip — the suite pins that — so the strip is
# not load-bearing for this set. It is kept for captcha_solver's static
# detector, whose own `cf-turnstile` spelling DOES match the injection.
BOT_CHALLENGE_MARKERS = {
    "recaptcha": ("recaptcha/api.js", "recaptcha/api2/anchor",
                  "recaptcha/api2/bframe", "recaptcha/enterprise.js"),
    "hcaptcha": ("hcaptcha.com/1/api.js", "newassets.hcaptcha.com"),
    "cloudflare": ("challenges.cloudflare.com", "_cf_chl_opt", "__cf_chl",
                   "/cdn-cgi/challenge-platform/h/"),
}

# Every page the site serves — listings, the registration and login pages,
# even its 404 — is built out of its own asset host: 25-28 references on
# every capture. An interstitial is not, and neither is Chromium's own
# network-error page, which carries the site's hostname in its <title> and
# would read as a real page to any text check.
_ASSET_HOST_RE = re.compile(r"cdn-static\.screener\.in")
MIN_ASSET_REFERENCES = 5

# The 2Captcha Scraping Browser's auto-solve extension injects its own
# hunters, one carrying data-ts-input="cf-turnstile-response", into every
# page it loads. BOT_CHALLENGE_MARKERS above does not need this; the static
# captcha detector in captcha_solver does, and imports it from here.
_EXTENSION_SCRIPT_RE = re.compile(
    r"<script\b[^>]*(?:chrome|moz)-extension://[^>]*>\s*</script>", re.IGNORECASE)

# Bounded, because a refusal page is small and a full listing is ~40 KB:
# html.unescape over the prefix makes an entity-escaped marker match the same
# way a browser-serialised one does (a raw HTTP body and page.content() spell
# the same page differently).
_MARKER_SCAN_BYTES = 64 * 1024


def strip_extension_scripts(html: str) -> str:
    """Remove browser-extension <script> tags, whole, before a marker scan."""
    return _EXTENSION_SCRIPT_RE.sub("", html)


def detect_bot_challenge(html: str) -> Optional[str]:
    """Vendor name if `html` carries a challenge's own loader, else None."""
    head = html_lib.unescape((html or "")[:_MARKER_SCAN_BYTES])
    for vendor, markers in BOT_CHALLENGE_MARKERS.items():
        if any(marker in head for marker in markers):
            return vendor
    return None


def asset_reference_count(html: str) -> int:
    """How many times the page references screener.in's own asset host."""
    return len(_ASSET_HOST_RE.findall(html or ""))


def count_cards(html: str) -> int:
    """How many distinct result rows the markup holds."""
    return len(set(_ROW_ID_RE.findall(html or "")))


# ---------------------------------------------------------------------------
# The login wall
# ---------------------------------------------------------------------------
# Not a captcha and not a refusal status: an HTTP 200 registration page. The
# 2026-08-23/24 captures taken through the Scraping Browser API and the
# Scraper API are both `<title>Register - Screener</title>` with a
# `<form ... action="/register/">`, for `/screens/` URLs that plain curl from
# a residential connection was served in full on 2026-09-25. The login page
# has the same shape with `action="/login/"`.
#
# Every served page links to /register/ from its navigation (2 occurrences on
# every listing capture), so the bare path is NOT a marker. What only the
# wall has is the form that posts to it.
_WALL_PATHS = ("/register/", "/login/")
_WALL_FORM_RE = re.compile(
    r"<form\b[^>]*\baction=[\"'](?:https?://[^/\"']+)?/(?:register|login)/", re.I)


def is_login_wall(html: Optional[str], url: Optional[str] = None) -> bool:
    """Whether this response is screener.in's registration or login page."""
    path = urlparse(url or "").path
    if any(path.startswith(p) for p in _WALL_PATHS):
        return True
    html = html or ""
    return bool(_WALL_FORM_RE.search(html)) and not _ROW_ID_RE.search(html)


# ---------------------------------------------------------------------------
# The site's own arithmetic
# ---------------------------------------------------------------------------
def page_info(html: Optional[str]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """(total_results, served_page, pages_available) as the page states them.

    Any of the three may be None when the page does not say. Read from the
    `data-page-info` element when it is there, and from the whole text
    otherwise, since a browser re-serialisation may move attributes around.
    """
    text = html or ""
    block = re.search(r"data-page-info[^>]*>(.*?)</div>", text, re.S | re.I)
    scope = re.sub(r"<[^>]+>", " ", block.group(1)) if block else text
    m = _PAGE_INFO_RE.search(scope)
    if m:
        return (int(m.group(1).replace(",", "")), int(m.group(2)),
                int(m.group(3)))
    m = _TOTAL_ONLY_RE.search(scope) if block else None
    if m:
        return int(m.group(1).replace(",", "")), None, None
    return None, None, None


def total_results(html: Optional[str]) -> Optional[int]:
    return page_info(html)[0]


def served_page(html: Optional[str]) -> Optional[int]:
    return page_info(html)[1]


def pages_available(html: Optional[str]) -> Optional[int]:
    return page_info(html)[2]


# ---------------------------------------------------------------------------
# Pagination and URL shape
# ---------------------------------------------------------------------------
_PAGE_PARAM = "page"


def requested_page_number(url: str) -> int:
    """Which page of the listing `url` asks the site for (1 when it says nothing).

    The URL's own number, not a loop counter: a run started on `?page=3`
    asks the site for page 3, and comparing what was served against the run's
    own "page 1" would call a correct page the end of the listing.
    """
    value = dict(parse_qsl(urlparse(url or "").query)).get(_PAGE_PARAM)
    try:
        number = int(value) if value is not None else 1
    except ValueError:
        return 1
    return number if number >= 1 else 1


def malformed_page_param(url: str) -> Optional[str]:
    """The raw `page` value when it is not a positive integer, else None.

    Worth refusing up front rather than following: `?page=abc` is served as
    page 1 and `?page=0` as the LAST page (both measured 2026-09-25).
    """
    query = dict(parse_qsl(urlparse(url or "").query, keep_blank_values=True))
    if _PAGE_PARAM not in query:
        return None
    raw = query[_PAGE_PARAM]
    return None if raw.isdigit() and int(raw) >= 1 else raw


def page_url(url: str, page_num: int) -> str:
    """`url` with the site's own `?page=` set to `page_num`.

    The site's own pagination links are `href="?page=2"` relative to the
    listing, and its "Next" link is the same URL — so this is the
    convention the site itself builds, not a guess. Every other query
    parameter (sort, order) is preserved, `page` is replaced rather than
    appended twice, and page 1 carries no page parameter at all, which is
    what the site's own "1" link does (`href="#"`).
    """
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != _PAGE_PARAM]
    if page_num > 1:
        query.append((_PAGE_PARAM, str(page_num)))
    return urlunparse(parts._replace(query=urlencode(query)))


# /screens/{id}/{slug}/ and /market/{sector}/{industry}/.../ — the two page
# kinds this repo reads, both measured. Anything else is refused WITH the
# reason, because "no rows" on a hub page reads as a broken scraper.
_SCREEN_RE = re.compile(r"^/screens/\d+/[^/]+/$")
_MARKET_RE = re.compile(r"^/market/(?:IN\d+/)+$")


def listing_kind(url: str) -> str:
    """"screen", "market", or what else the URL is."""
    path = urlparse(url or "").path or "/"
    if not path.endswith("/"):
        path += "/"
    if _SCREEN_RE.match(path):
        return "screen"
    if _MARKET_RE.match(path):
        return "market"
    if path == "/market/":
        return "market_index"
    if path.startswith("/company/"):
        return "company"
    if path.startswith("/screens/raw/"):
        return "raw_query"
    if path.startswith("/screens/"):
        return "screens_index"
    return "other"


def unsupported_reason(url: str) -> Optional[str]:
    """Why this repo cannot read `url` as a listing, or None when it can."""
    kind = listing_kind(url)
    if kind in ("screen", "market"):
        return None
    if kind == "market_index":
        return ("/market/ is the industries overview — a table of sectors, "
                "not of companies. Pass one sector or industry from it, e.g. "
                "https://www.screener.in/market/IN08/IN0801/IN080101/")
    if kind == "company":
        return ("that URL is one company's page; this repo reads result "
                "tables (screens and sector listings) and does not implement "
                "the company page")
    if kind == "raw_query":
        return ("/screens/raw/?query=... answered HTTP 404 to an anonymous "
                "visitor on 2026-09-25; save the query as a screen and pass "
                "its /screens/{id}/{slug}/ URL instead")
    if kind == "screens_index":
        return ("that is a list of screens, not a screen. Pass one of them, "
                "e.g. https://www.screener.in/screens/59/magic-formula/")
    return ("not a screener.in screen (/screens/{id}/{slug}/) or sector "
            "listing (/market/IN.../)")


def listing_title(html: Optional[str]) -> Optional[str]:
    """The listing's own heading: "IT - Software Companies", "Magic Formula".

    This fills the `category` column when no --category was given, for both
    page kinds alike: a sector URL is a chain of codes (IN08/IN0801/IN080101)
    that means nothing to a reader, and a screen's slug is a lower-cased
    approximation of what the page itself says.
    """
    m = re.search(r"<h1\b[^>]*>(.*?)</h1>", html or "", re.S | re.I)
    if not m:
        return None
    text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip()
    return text or None


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------
def to_number(text: Optional[str]) -> Optional[float]:
    """A table cell as a number, or None for an empty or non-numeric cell.

    Every numeric cell on every capture is a bare decimal with an optional
    leading minus ("754389.23", "-20.98"); empty cells ("") are the site
    saying it has no figure, and are None rather than 0. Thousands commas
    are tolerated in case a deploy adds them, but a cell that is not a number
    at all stays None rather than being coerced.
    """
    if text is None:
        return None
    raw = text.strip().replace(",", "").replace(" ", "")
    if not raw or raw in ("-", "--", "—"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _rank(text: str) -> Optional[int]:
    m = re.match(r"^\s*(\d+)\.?\s*$", text or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------
# The site's own metric name (the header's data-tooltip) -> this repo's
# column, WITH the unit the column's name promises. A header whose unit
# differs is not mapped: a "Market Capitalization" in some other unit written
# into `market_cap_cr` would be a wrong number wearing a right name. It goes
# to `extra_metrics` under its own name instead, and a warning says so.
# These nine are the columns an anonymous visitor sees on every page kind
# measured; a screen can add more, which land in `extra_metrics`.
FIXED_COLUMNS: Dict[str, Tuple[str, str]] = {
    "Current Price": ("price", "Rs."),
    "Price to Earning": ("pe", ""),
    "Market Capitalization": ("market_cap_cr", "Rs.Cr."),
    "Dividend yield": ("dividend_yield_pct", "%"),
    "Net Profit latest quarter": ("net_profit_qtr_cr", "Rs.Cr."),
    "YOY Quarterly profit growth": ("qtr_profit_var_pct", "%"),
    "Sales latest quarter": ("sales_qtr_cr", "Rs.Cr."),
    "YOY Quarterly sales growth": ("qtr_sales_var_pct", "%"),
    "Return on capital employed": ("roce_pct", "%"),
}

# "Rs." is the only unit the price column has carried, and it names the
# rupee. It is the page's own written unit — not a compiled-in default — so a
# page whose price header says anything else leaves `currency` null.
_CURRENCY_BY_UNIT = {"Rs.": "INR"}


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _header_columns(header_row) -> List[Optional[Tuple[str, str]]]:
    """[(metric_name, unit) or None] per header cell, in order.

    None for the two cells that are not metrics (S.No. and the company
    name). The name is the `data-tooltip`; a header without one falls back
    to its visible label minus the unit, so a deploy that drops the tooltip
    still yields a readable key rather than a positional one.
    """
    columns: List[Optional[Tuple[str, str]]] = []
    for th in header_row.find_all("th", recursive=False):
        tooltip = th.get("data-tooltip")
        span = th.find("span")
        unit = _norm(span.get_text()) if span else ""
        if tooltip:
            columns.append((_norm(tooltip), unit))
            continue
        label = _norm(th.get_text(" "))
        if unit and label.endswith(unit):
            label = label[: -len(unit)].strip()
        if label.lower().rstrip(".") in ("s.no", "sno", "company", "name", ""):
            columns.append(None)
        else:
            columns.append((label, unit))
    return columns


def _extra_key(name: str, unit: str) -> str:
    return f"{name} ({unit})" if unit else name


def _results_table(soup):
    """The table holding the result rows, or None.

    Chosen by what it CONTAINS — a row carrying the site's own
    data-row-company-id — not by its class, and not by being the first
    table: /market/ itself has a table, of industries.
    """
    for table in soup.find_all("table"):
        if table.find("tr", attrs={"data-row-company-id": True}):
            return table
    return None


def parse_products(html: str, base_url: str, category: Optional[str] = None,
                   page: Optional[int] = None) -> List[Product]:
    """Rows for one listing page, in the site's own order.

    `page` is the SITE's page number (what `?page=` asked for), so `page`
    and `position` are unique together across a run and `rank` can be
    checked against them.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    table = _results_table(soup)
    if table is None:
        return []

    header = None
    for tr in table.find_all("tr"):
        if tr.find("th") is not None and not tr.get("data-row-company-id"):
            header = tr
            break
    columns = _header_columns(header) if header is not None else []
    if not columns:
        logger.warning("The results table has no header row — rows are "
                       "reported with their company fields only, because a "
                       "metric read by position alone could land in the "
                       "wrong column.")

    unmapped_units = set()
    currency = None
    for col in columns:
        if col and col[0] == "Current Price":
            currency = _CURRENCY_BY_UNIT.get(col[1])

    label = category if category is not None else listing_title(html)
    rows: List[Product] = []
    seen = set()
    for tr in table.find_all("tr", attrs={"data-row-company-id": True}):
        company_id = tr.get("data-row-company-id", "").strip()
        if not company_id or company_id in seen:
            continue
        cells = tr.find_all("td", recursive=False)
        link = None
        for a in tr.find_all("a", href=True):
            path = urlparse(urljoin(base_url, a["href"])).path
            if _COMPANY_PATH_RE.match(path):
                link = a
                break
        if link is None:
            # A row with the site's id and no company link is not something
            # any capture has shown. Say so instead of emitting a row that
            # cannot be joined to a company page.
            logger.warning("Skipping row %s: it carries no /company/ link.",
                           company_id)
            continue
        seen.add(company_id)
        href = urljoin(base_url, link["href"])
        m = _COMPANY_PATH_RE.match(urlparse(href).path)
        ticker = m.group(1)

        row = Product(
            url=href,
            sku=company_id,
            title=_norm(link.get_text(" ")) or None,
            currency=currency,
            category=label,
            page=page,
            position=len(rows) + 1,
            rank=_rank(cells[0].get_text()) if cells else None,
            ticker=ticker,
            consolidated=bool(m.group(2)),
        )

        extras: Dict[str, Optional[float]] = {}
        if columns and len(cells) == len(columns):
            for col, cell in zip(columns, cells):
                if col is None:
                    continue
                name, unit = col
                value = to_number(cell.get_text())
                fixed = FIXED_COLUMNS.get(name)
                if fixed and fixed[1] == unit:
                    setattr(row, fixed[0], value)
                else:
                    if fixed:
                        unmapped_units.add((name, unit, fixed[1]))
                    extras[_extra_key(name, unit)] = value
        elif columns:
            logger.warning("Row %s has %d cells against %d header cells — its "
                           "metrics are left empty rather than read by a "
                           "position that does not line up.",
                           company_id, len(cells), len(columns))
        row.extra_metrics = extras or None
        rows.append(row)

    for name, unit, expected in sorted(unmapped_units):
        logger.warning("Column %r is in %r here, not %r — reported under "
                       "extra_metrics instead of its usual column.",
                       name, unit or "(no unit)", expected or "(no unit)")
    return rows


def rank_gaps(products: List[Product]) -> List[Tuple[int, int]]:
    """Missing rank ranges in a MERGED run, as [(first_missing, last_missing)].

    The site numbers its rows globally (page 2 starts at 26), so rows that
    never arrived are arithmetic rather than a guess — checked after merging
    pages, because a per-page check cannot see a gap BETWEEN pages. Only the
    span the run actually covered is checked: a 3-page window of a 7-page
    listing is not missing ranks 76-164.
    """
    ranks = sorted({p.rank for p in products if isinstance(p.rank, int)})
    gaps = []
    for a, b in zip(ranks, ranks[1:]):
        if b - a > 1:
            gaps.append((a + 1, b - 1))
    return gaps
