#!/usr/bin/env python3
"""
smoke_test.py
--------------
The offline suite: one file of plain functions, no pytest, no conftest.
`tests/test_smoke.py` wraps this as a single pytest test so `pytest` works as
an entry point without a second copy of the checks.

    python3 smoke_test.py

It must pass with NO engine library installed at all — every
`import playwright_scraper` / `puppeteer_scraper` / `selenium_scraper` is
guarded and the skip is recorded and printed. CI's `engine-smoke` job installs
each engine in its own virtualenv and fails if that engine's group reports
skipped, because "skipped, engine absent" reads identically to a real import
error.

**The fixtures are real captures, trimmed, verified and scrubbed**, and they
live in `fixtures_generated.json`, which `make_fixtures.py` builds from the
raw captures (never committed). Each trimmed fixture was checked to parse
identically, row for row, to its untrimmed original and to classify the same
way. NOT verbatim in them: the csrf token and cookie value (replaced with
SCRUBBED-CSRF-TOKEN) and a screen author's name and profile link (replaced
with SCRUBBED-AUTHOR and /user/0/). The fixtures are real, public stock data
as screener.in served it on 2026-09-25 (and, for the Scraping Browser and
Scraper API captures, 2026-08-23/24).

The few inputs that are NOT captures — a captcha widget for the solver's own
unit checks, a synthetic "0 results found" page — are built inline, say so
where they are built, and never stand in for a measurement: no capture of
either exists, because screener.in configures no captcha and no empty
listing was found to capture.
"""

import ast
import csv
import inspect
import io
import json
import logging
import os
import re
import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import asdict

import captcha_solver
import cli_types
import env_config
import output_writer
import page_flow
import product_parser
import proxy_pool
from output_writer import (EXIT_BLOCKED, EXIT_FETCH_FAILED, EXIT_NO_PRODUCTS,
                           EXIT_PARTIAL, Product, dedupe_by_sku, finish_run,
                           save, write_csv)
from product_parser import page_url, parse_products

REPO = os.path.dirname(os.path.abspath(__file__))

# Engine modules are optional: the suite has to pass with none of the driver
# libraries installed. What is NOT optional is that each engine imports its
# driver at MODULE level — see check_engine_imports_driver_at_module_level.
ENGINES = {}
SKIPPED_GROUPS = []
for _name in ("playwright_scraper", "puppeteer_scraper", "selenium_scraper"):
    try:
        ENGINES[_name] = __import__(_name)
    except ImportError as exc:
        SKIPPED_GROUPS.append(f"{_name} ({exc.name or exc})")

PASSED = []
FAILED = []
SKIPPED = []


def check(label, condition):
    (PASSED if condition else FAILED).append(label)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    return bool(condition)


def skip(label, why):
    """Record a check that could NOT be made.

    Deliberately not `check(label, True)`: a check asserted True because its
    input was missing is indistinguishable in the output from one that ran.
    """
    SKIPPED.append(f"{label} ({why})")
    print(f"  SKIP  {label} — {why}")
    return False


def eq(label, actual, expected):
    ok = actual == expected
    if not ok:
        label = f"{label} (got {actual!r}, expected {expected!r})"
    return check(label, ok)


# HERMETIC: this suite drives the engines' real parse_args, which read the
# environment and .env. A developer's own SCREENER_PROXY would otherwise make
# an "offline" check send a real preflight request. So every variable the
# code reads is cleared and .env loading is disabled for the whole run; the
# .env checks below set exactly what they test.
for _key in list(env_config.ENV_KEYS):
    os.environ.pop(_key, None)
env_config.load_env = lambda *a, **k: None

with open(os.path.join(REPO, "fixtures_generated.json"), encoding="utf-8") as _f:
    FIXTURES = json.load(_f)


def fx(name):
    """(html, url) of one committed fixture."""
    return FIXTURES[name]["html"], FIXTURES[name]["url"]


def rows_of(name, **kw):
    html, url = fx(name)
    return parse_products(html, url, page=product_parser.requested_page_number(url), **kw)


def state_of(name, **kw):
    html, url = fx(name)
    return page_flow.classify(html, url=url, **kw)


def _row(**kw):
    base = dict(sku="1", url="https://www.screener.in/company/TCS/", price=1.0)
    base.update(kw)
    return Product(**base)


# ---------------------------------------------------------------------------
# 1. The parser, on real captures — VALUES, not coverage
# ---------------------------------------------------------------------------
def check_parser_values():
    print("\n[parser values]")
    rows = rows_of("market_p1")
    # The fixture keeps rows 1-3 and the page's LAST row (25).
    eq("the trimmed sector page parses to its 4 kept rows", len(rows), 4)
    a = rows[0]
    eq("sku is the site's own data-row-company-id", a.sku, "3365")
    eq("title is the company name the table links", a.title, "TCS")
    eq("url is the company's own page, absolute",
       a.url, "https://www.screener.in/company/TCS/consolidated/")
    eq("ticker comes from that URL", a.ticker, "TCS")
    eq("and the /consolidated/ view is recorded", a.consolidated, True)
    eq("rank is the site's own S.No.", a.rank, 1)
    eq("CMP", a.price, 2085.05)
    eq("currency from the price column's own unit", a.currency, "INR")
    eq("P/E", a.pe, 14.03)
    eq("market cap in crore", a.market_cap_cr, 754389.23)
    eq("dividend yield", a.dividend_yield_pct, 3.07)
    eq("net profit latest quarter", a.net_profit_qtr_cr, 13420.0)
    eq("YoY quarterly profit growth", a.qtr_profit_var_pct, 8.45)
    eq("sales latest quarter", a.sales_qtr_cr, 72275.0)
    eq("YoY quarterly sales growth", a.qtr_sales_var_pct, 13.93)
    eq("ROCE", a.roce_pct, 63.03)
    eq("a sector page shows no column beyond the nine", a.extra_metrics, None)
    eq("category is the page's own heading when none was given",
       a.category, "IT - Software Companies")
    eq("an explicit --category wins",
       rows_of("market_p1", category="mine")[0].category, "mine")
    eq("the last kept row is rank 25 — the repeated header row was skipped",
       rows[-1].rank, 25)
    eq("page and position", (a.page, a.position, rows[-1].position), (1, 1, 4))

    # A screen with more columns than the nine: nothing is thrown away.
    m = rows_of("magic_formula_p1")[1]
    eq("a six-digit ticker is kept as the URL spells it", m.ticker, "532015")
    eq("and a standalone view is recorded as such", m.consolidated, False)
    eq("the extra columns land under the site's own names and units",
       m.extra_metrics, {"Return on invested capital (%)": 88.05,
                         "Earnings yield (%)": 22.28, "Book value (Rs.)": 1.91})
    eq("while the nine still fill their own columns",
       (m.price, m.roce_pct), (14.48, 158.68))
    eq("category from a screen is its heading", m.category, "Magic Formula")

    # Page 2's ranks continue page 1's: the numbering is global.
    eq("page 2 starts at rank 26", rows_of("market_p2")[0].rank, 26)
    eq("and its rows carry page 2", rows_of("market_p2")[0].page, 2)
    last = rows_of("all_stocks_p220")
    eq("page 220 of All Stocks holds the 2 rows the site's arithmetic says",
       [r.rank for r in last], [5476, 5477])

    # An empty cell is the site saying it has no figure — None, never 0.
    cells = [r for r in rows_of("golden_crossover")]
    check("a zero is kept as a real zero where the site prints 0.00",
          any(r.dividend_yield_pct == 0.0 for r in rows_of("magic_formula_p1")))
    eq("to_number reads an empty cell as None", product_parser.to_number(""), None)
    eq("and a negative as negative", product_parser.to_number("-20.98"), -20.98)
    eq("and does not coerce text", product_parser.to_number("n/a"), None)
    eq("golden crossover parses all 16 of its rows", len(cells), 16)


def check_parser_refuses_to_guess():
    print("\n[parser guards]")
    html, url = fx("market_p1")
    # A header whose unit is not the one the column promises is NOT mapped.
    changed = html.replace('Mar Cap\n                    <span style="color: hsl(0, 0%, 45%)">Rs.Cr.</span>',
                           'Mar Cap\n                    <span style="color: hsl(0, 0%, 45%)">USD mn</span>')
    check("control: the unit edit actually applied", changed != html)
    row = parse_products(changed, url, page=1)[0]
    eq("a market cap in another unit is not written into market_cap_cr",
       row.market_cap_cr, None)
    eq("it is kept under its own name and unit instead",
       (row.extra_metrics or {}).get("Market Capitalization (USD mn)"), 754389.23)

    # A price header in another currency leaves currency null, never INR.
    no_rs = html.replace('CMP\n                    <span style="color: hsl(0, 0%, 45%)">Rs.</span>',
                         'CMP\n                    <span style="color: hsl(0, 0%, 45%)">$</span>')
    check("control: the currency edit actually applied", no_rs != html)
    eq("an unknown price unit leaves currency null",
       parse_products(no_rs, url, page=1)[0].currency, None)

    # A row whose cell count does not line up with the header keeps its
    # company fields and loses its metrics, rather than shifting them.
    short = re.sub(r"(<tr data-row-company-id=\"3365\">.*?)<td>2085\.05</td>", r"\1",
                   html, count=1, flags=re.S)
    check("control: the cell removal actually applied", short != html)
    r = parse_products(short, url, page=1)[0]
    eq("a misaligned row keeps its identity", r.sku, "3365")
    eq("and gets no metric read by a position that does not line up",
       (r.price, r.pe, r.market_cap_cr), (None, None, None))

    eq("a page with no results table parses to nothing",
       parse_products(fx("register")[0], "https://www.screener.in/register/"), [])
    eq("/market/'s own table of INDUSTRIES is not read as companies",
       parse_products(*fx("market_index")), [])


def check_page_info_and_pagination():
    print("\n[pagination]")
    eq("the site's own arithmetic is read",
       product_parser.page_info(fx("market_p1")[0]), (164, 1, 7))
    eq("including a 5,477-result listing's last page",
       product_parser.page_info(fx("all_stocks_p220")[0]), (5477, 220, 220))
    base = "https://www.screener.in/market/IN08/IN0801/IN080101/"
    eq("page 1 carries no page parameter", page_url(base, 1), base)
    eq("page 2 is ?page=2, the site's own link", page_url(base, 2), base + "?page=2")
    eq("other parameters survive and page is replaced, not doubled",
       page_url(base + "?sort=name&order=desc&page=5", 3),
       base + "?sort=name&order=desc&page=3")
    eq("the requested page is read from the URL",
       product_parser.requested_page_number(base + "?page=4"), 4)
    eq("and is 1 when the URL says nothing",
       product_parser.requested_page_number(base), 1)
    eq("a malformed page value is named", product_parser.malformed_page_param(base + "?page=abc"), "abc")
    eq("page=0 is malformed too (the site serves its LAST page for it)",
       product_parser.malformed_page_param(base + "?page=0"), "0")
    eq("a normal page value is fine", product_parser.malformed_page_param(base + "?page=3"), None)

    eq("a run started on ?page=3 asks for site pages 3, 4, 5",
       [page_flow.site_page(3, n) for n in (1, 2, 3)], [3, 4, 5])
    eq("and builds their URLs from the site page",
       page_flow.url_for(base + "?page=3", 3, 2), base + "?page=4")
    eq("planning never goes past the site's own page count",
       page_flow.plan_pages(50, 1, 7), 7)
    eq("counting from the start page", page_flow.plan_pages(50, 5, 7), 3)
    eq("and asks for what was asked when the site states nothing",
       page_flow.plan_pages(4, 1, None), 4)
    check("reaching page 7 of 7 is the last page", page_flow.reached_last_page(1, 7, 7))
    check("page 3 of 7 is not", not page_flow.reached_last_page(1, 3, 7))



def check_page_states():
    print("\n[page states]")
    for name, want, vendor in (
            ("market_p1", page_flow.CONTENT, None),
            ("market_p2", page_flow.CONTENT, None),
            ("market_p7_last", page_flow.CONTENT, None),
            ("all_stocks_p220", page_flow.CONTENT, None),
            ("golden_crossover", page_flow.CONTENT, None),
            ("magic_formula_p1", page_flow.CONTENT, None),
            ("market_p8_out_of_range", page_flow.EXHAUSTED, None),
            ("not_found", page_flow.NOT_FOUND, "not_found"),
            ("register", page_flow.BLOCKED, "login_wall"),
            ("login", page_flow.BLOCKED, "login_wall"),
            ("cdp_register_wall", page_flow.BLOCKED, "login_wall"),
            ("scraperapi_register_wall", page_flow.BLOCKED, "login_wall"),
            ("chromium_proxy_error", page_flow.BLOCKED, "unknown")):
        st = state_of(name)
        eq(f"{name} is {want}" + (f" ({vendor})" if vendor else ""),
           (st.state, st.vendor), (want, vendor))

    # The out-of-range page is FULL of real rows. What makes it the end of
    # the listing is only the site's own statement of which page it served.
    oor_html, oor_url = fx("market_p8_out_of_range")
    check("the out-of-range page carries real rows",
          product_parser.count_cards(oor_html) > 0)
    eq("which are page 7's", product_parser.served_page(oor_html), 7)
    eq("and the same page asked for AS page 7 is content",
       page_flow.classify(oor_html, url=oor_url, requested_page=7).state,
       page_flow.CONTENT)
    eq("the requested page comes from the caller, not the URL, when given",
       page_flow.classify(fx("market_p2")[0], url="https://x/", requested_page=2).state,
       page_flow.CONTENT)

    # A login redirect is recognised from the URL alone, too.
    eq("a browser that ended on /register/ is at the wall",
       page_flow.classify("<html></html>", url="https://www.screener.in/register/?next=/screens/1/x/").vendor,
       "login_wall")
    # Every served page LINKS to /register/: the bare path is not a marker.
    check("a listing's own /register/ nav link does not read as the wall",
          "/register/" in fx("market_p1")[0] and state_of("market_p1").state == page_flow.CONTENT)

    # Refusal statuses outrank the heuristics; a 404 is its own state.
    eq("a 429 is blocked (http)",
       page_flow.classify("<html></html>", status_code=429).vendor, "http")
    eq("a 500 is a server error, not a block (and not 'check the proxy')",
       page_flow.classify("<html></html>", status_code=500).state, page_flow.SERVER_ERROR)
    eq("while a 503 stays a refusal", page_flow.classify("<html></html>", status_code=503).state,
       page_flow.BLOCKED)
    eq("a 404 status is not_found, not blocked",
       page_flow.classify("<html></html>", status_code=404).state, page_flow.NOT_FOUND)
    check("not_found is not retried, not paid for, and not exit 3",
          not any((page_flow.STATE_POLICY[page_flow.NOT_FOUND].retry,
                   page_flow.STATE_POLICY[page_flow.NOT_FOUND].may_solve,
                   page_flow.STATE_POLICY[page_flow.NOT_FOUND].blocked)))

    # Synthetic, labelled: no zero-result listing was found to capture. The
    # site's own sentence, taken from the real template's wording.
    empty = fx("market_p1")[0].replace("164 results found: Showing page 1 of 7",
                                       "0 results found")
    check("control: the synthetic edit actually applied", empty != fx("market_p1")[0])
    eq("a page stating 0 results is EMPTY (synthetic input — unmeasured on "
       "the live site)", page_flow.classify(empty, url=fx("market_p1")[1]).state,
       page_flow.EMPTY)

    # A served page with the site's count but no rows is OUR bug.
    no_rows = re.sub(r"<tr data-row-company-id=.*?</tr>", "", fx("market_p1")[0], flags=re.S)
    st = page_flow.classify(no_rows, url=fx("market_p1")[1])
    eq("rows gone but the count stated is CONTENT, so a parse failure is "
       "reported rather than an empty listing", st.state, page_flow.CONTENT)


def check_markers_on_good_and_cdp_pages():
    print("\n[markers]")
    # The family rule: count a marker on a page you know is good before
    # trusting it — and on a page fetched the way a real run fetches.
    good = [n for n in FIXTURES if state_of(n).state in (page_flow.CONTENT, page_flow.EXHAUSTED)]
    check(f"there are good pages to count on ({len(good)})", len(good) >= 6)
    hits = [n for n in good if product_parser.detect_bot_challenge(fx(n)[0])]
    eq("no challenge marker fires on any served listing", hits, [])
    cdp = fx("cdp_register_wall")[0]
    eq("the Scraping Browser capture carries the extension's 16 injected scripts",
       cdp.count("chrome-extension://"), 16)
    check("including the cf-turnstile-response spelling that fooled a sibling",
          "cf-turnstile-response" in cdp)
    eq("the marker set scores zero on it WITHOUT the extension strip — so the "
       "strip is not load-bearing for this set",
       product_parser.detect_bot_challenge(cdp), None)
    eq("the static captcha detector reads it as no captcha (with its strip)",
       captcha_solver.detect_in_html(cdp, fx("cdp_register_wall")[1]), None)
    stripped_away = product_parser.strip_extension_scripts(cdp)
    eq("and the strip removes every injected script, whole",
       stripped_away.count("chrome-extension://"), 0)
    eq("an empty autosolver mount is not a challenge",
       captcha_solver.detect_autosolver_mount(cdp), None)
    counts = {n: product_parser.asset_reference_count(fx(n)[0]) for n in FIXTURES}
    check("every page the site built references its asset host at least "
          f"{product_parser.MIN_ASSET_REFERENCES} times",
          all(c >= product_parser.MIN_ASSET_REFERENCES
              for n, c in counts.items() if n != "chromium_proxy_error"))
    eq("and Chromium's own error page references it zero times",
       counts["chromium_proxy_error"], 0)
    check("even though it carries the site's hostname in its title",
          "<title>www.screener.in</title>" in fx("chromium_proxy_error")[0])


def check_shortfall_and_readiness():
    print("\n[arithmetic]")
    eq("a full page expects 25", page_flow.expected_records(164, 1), 25)
    eq("the last page expects the remainder", page_flow.expected_records(164, 7), 14)
    eq("past the end expects nothing", page_flow.expected_records(164, 8), 0)
    check("a short page is named",
          page_flow.shortfall(164, 1, 18) is not None)
    eq("a full one is not", page_flow.shortfall(164, 7, 14), None)
    eq("the readiness floor applies to a full page", page_flow.min_matches(5, 164, 1), 5)
    eq("and drops to what a 2-row last page holds",
       page_flow.min_matches(5, 5477, 220), 2)
    eq("and to 1 for a one-row page — the selector is the site's own row "
       "attribute, not a generic link", page_flow.min_matches(5, 26, 2), 1)
    eq("and stays at the floor when the site states nothing",
       page_flow.min_matches(5, None, 1), 5)

    gaps = product_parser.rank_gaps([_row(sku=str(n), rank=n) for n in (1, 2, 3, 7, 8, 12)])
    eq("rank gaps in a merged run are arithmetic", gaps, [(4, 6), (9, 11)])
    eq("a contiguous run has none",
       product_parser.rank_gaps([_row(sku=str(n), rank=n) for n in range(26, 51)]), [])


def check_url_refusals():
    print("\n[url refusals]")
    ok = ("https://www.screener.in/screens/59/magic-formula/",
          "https://www.screener.in/market/IN08/",
          "https://www.screener.in/market/IN08/IN0801/IN080101/?page=2")
    for url in ok:
        eq(f"{url} is readable", product_parser.unsupported_reason(url), None)
    for url, word in (("https://www.screener.in/market/", "industries overview"),
                      ("https://www.screener.in/company/TCS/consolidated/", "company"),
                      ("https://www.screener.in/screens/raw/?query=x", "404"),
                      ("https://www.screener.in/screens/", "list of screens"),
                      ("https://www.screener.in/explore/", "not a screener.in")):
        reason = product_parser.unsupported_reason(url) or ""
        check(f"{url} is refused WITH the reason ({word!r})", word in reason)

    import argparse as _argparse
    parser = _argparse.ArgumentParser()
    class Args:
        pass
    for bad in ("https://www.screener.in/market/", "https://www.screener.in/market/IN08/?page=0"):
        args = Args()
        args.url, args.category, args.pages = bad, None, 1
        try:
            with redirect_stdout(io.StringIO()), \
                    __import__("contextlib").redirect_stderr(io.StringIO()):
                cli_types.finish_args(parser, args, logging.getLogger("t"))
            check(f"{bad} is refused at the command line", False)
        except SystemExit as e:
            eq(f"{bad} is refused at the command line as bad usage", e.code, 2)
    args = Args()
    args.url, args.category, args.pages = "https://www.screener.in/screens/59/magic-formula/?page=2", None, 2
    cli_types.finish_args(parser, args, logging.getLogger("t"))
    eq("a URL starting on page 2 starts the run there", args.start_page, 2)
    eq("and the category is left to the page's own heading", args.category, None)
def check_fingerprint_kwargs_are_ones_playwright_accepts():
    print("\n[fingerprint kwargs]")
    # An unknown key in new_context(**kwargs) is a TypeError at launch, on the
    # PAID path, at runtime. Checked against the driver's real
    # signature rather than against a list written from memory.
    import fingerprint_client
    sample = {"id": "x", "country": "de", "userAgent": {"value": "UA/1"},
              "screen": {"width": 1920, "height": 1080}, "locale": "de-DE",
              "timezone": "Europe/Berlin",
              "navigator": {"platform": "Win32", "hardwareConcurrency": 8},
              "webgl": {"vendor": "Google Inc.", "renderer": "ANGLE"}}
    kwargs = fingerprint_client.playwright_context_kwargs(sample)
    check("the fingerprint produces some context kwargs at all", bool(kwargs))
    engine = ENGINES.get("playwright_scraper")
    if engine is None:
        skip("fingerprint kwargs vs the real new_context signature",
             "playwright not installed")
        return
    from playwright.sync_api import Browser
    accepted = set(inspect.signature(Browser.new_context).parameters)
    unknown = sorted(set(kwargs) - accepted)
    check("every fingerprint kwarg is one Browser.new_context accepts"
          + (f" (unknown: {unknown})" if unknown else ""), not unknown)
    # And the init script must be valid JS shape, since a syntax error there
    # fails silently inside the browser.
    script = fingerprint_client.playwright_init_script(sample)
    check("the init script bakes its values in as JSON",
          '"platform": "Win32"' in script or '"platform":"Win32"' in script)
    check("and patches both WebGL contexts",
          "WebGL2RenderingContext" in script and "37446" in script)




def check_credentials_never_leak():
    print("\n[credentials]")
    masked = proxy_pool.mask("http://user:secret@gate.example.com:9999")
    check("a masked proxy keeps host and port", "gate.example.com:9999" in masked)
    check("and drops the credentials",
          "secret" not in masked and "user" not in masked)
    pw = proxy_pool.to_playwright("http://user:secret@gate.example.com:9999")
    check("credentials never reach the browser's argv",
          "secret" not in pw["server"] and "user" not in pw["server"])
    eq("they go in their own fields instead", (pw["username"], pw["password"]),
       ("user", "secret"))
    arg, creds = proxy_pool.to_pyppeteer("http://user:secret@gate.example.com:9999")
    check("pyppeteer's switch carries no credentials", "secret" not in arg)
    eq("they go through page.authenticate instead", creds["password"], "secret")
    arg, warning = proxy_pool.to_selenium("http://user:secret@gate.example.com:9999")
    check("Selenium's switch carries no credentials", "secret" not in arg)
    check("and the user is WARNED rather than misled",
          warning and "cannot authenticate" in warning)

    # An exception message is a log. The v1 captcha endpoint and the
    # fingerprint API both take the key as a query parameter.
    leaky = ("HTTPSConnectionPool: /res.php?key=" + "0123456789abcdef" * 2 +
             "&action=get failed; also ws://login:" + "pass@cb.2captcha.com:9222")
    redacted = proxy_pool.redact_secret_patterns(leaky)
    check("a key in a query string is redacted",
          "0123456789abcdef" not in redacted)
    check("a password in a URL is redacted", "pass@" not in redacted)
    check("the endpoint itself survives redaction",
          "res.php" in redacted and "cb.2captcha.com:9222" in redacted)
    eq("a password that itself contains '@' is masked whole",
       proxy_pool.redact_secret_patterns("http://user:p@ss@host:9999/x"), "http://***:***@host:9999/x")
    check("a URL carrying only a token before '@' is masked",
          "TOKEN123456" not in proxy_pool.redact_secret_patterns("wss://TOKEN123456@host:1"))
    check("a Bearer header value is masked",
          "abcdef123456" not in proxy_pool.redact_secret_patterns("Authorization: Bearer abcdef123456"))
    check("and an ordinary URL is left alone",
          proxy_pool.redact_secret_patterns("https://www.screener.in/company/TCS/")
          == "https://www.screener.in/company/TCS/")
    # Globally, not once.
    twice = proxy_pool.redact_secret_patterns("key=aaa and key=bbb")
    check("every occurrence is redacted, not just the first",
          "aaa" not in twice and "bbb" not in twice)

    for bad, why in (("gate.example.com:9999", "a bare host:port is refused"),
                     ("socks5://user:pass@host:1080",
                      "an authenticated socks5 exit is refused, not silently stripped")):
        try:
            proxy_pool.parse_proxy_line(bad)
            check(why, False)
        except proxy_pool.ProxyError as e:
            check(why, True)
            check("and the refusal itself carries no credentials",
                  "pass" not in str(e).replace("password", ""))


def check_remote_connect_failures_are_redacted():
    print("\n[remote connect failures]")
    # Measured live on 2026-09-19 against a real Scraping Browser endpoint
    # that answered 401: Playwright put the endpoint — password included —
    # into the exception message AND into a four-line call log under it, five
    # occurrences in one traceback. An exception message is a log, and a
    # traceback printed on the way out is a log too.
    # Assembled from pieces so this file carries no credential-shaped
    # literal for the repo's own secret scan to (rightly) fail on.
    secret = "ws://acct-zone-scraping_browser-pid-1:" + "s3cr3tpassw0rd@cb.2captcha.com:9222"
    library_error = ("connect_over_cdp: WebSocket error\n"
                     f"  - <ws unexpected response> {secret}/ 401 Unauthorized\n"
                     f"  - <ws error> {secret}/ closed before established\n"
                     f"  - <ws connect error> {secret}/ closed\n")
    redacted = proxy_pool.redact_secret_patterns(library_error)
    check("a credentialled endpoint is redacted out of a library error",
          "s3cr3tpassw0rd" not in redacted)
    eq("every occurrence of it, not just the first",
       redacted.count("***:***@cb.2captcha.com:9222"), 3)
    check("and the endpoint, the host and the status all survive",
          "cb.2captcha.com:9222" in redacted and "401 Unauthorized" in redacted)

    for name in ("playwright_scraper", "puppeteer_scraper", "selenium_scraper"):
        source = open(os.path.join(REPO, f"{name}.py"), encoding="utf-8").read()
        check(f"{name} wraps its remote connect rather than letting the "
              f"library's own error escape",
              re.search(r"Could not (?:connect to --cdp-endpoint|attach to)[^\n]*\n?[^\n]*"
                        r"redact_secret_patterns\(str\(e\)\)", source) is not None)
        check(f"{name} redacts a traceback before printing it",
              "redact_secret_patterns(traceback.format_exc())" in source)

    engine = ENGINES.get("playwright_scraper")
    if engine is None:
        skip("the engine's own connect path redacts", "playwright not installed")
        return

    class _StubChromium:
        def connect_over_cdp(self, endpoint, timeout=None):
            raise RuntimeError(library_error)

    class _StubPW:
        chromium = _StubChromium()

    class Args:
        cdp_endpoint = secret

    try:
        engine._connect_remote(_StubPW(), Args())
        check("the engine refuses a failed connect", False)
    except RuntimeError as e:
        check("the engine's own connect failure carries no password",
              "s3cr3tpassw0rd" not in str(e))
        check("and still names what failed and where",
              "cb.2captcha.com:9222" in str(e))


def check_proxy_rotation():
    print("\n[proxy rotation]")
    pool = proxy_pool.ProxyPool(["http://a:1", "http://b:2", "http://c:3"],
                                rotate="per-page")
    eq("starts on the first exit", pool.current, "http://a:1")
    pool.advance("test")
    eq("advances in order", pool.current, "http://b:2")
    pool.advance("test")
    pool.advance("test")
    eq("wraps rather than exhausting", pool.current, "http://a:1")
    eq("counts its rotations", pool.rotations, 3)
    single = proxy_pool.ProxyPool(["http://a:1"])
    single.advance("nowhere to go")
    eq("a one-exit pool stays put", single.current, "http://a:1")
    eq("and does not claim a rotation", single.rotations, 0)
    check("per-run does not rotate per page", not single.rotates_per_page())
    copy = pool.proxies
    copy.append("http://d:4")
    eq("the pool hands out a copy, so a worker cannot mutate it", len(pool), 3)




def check_proxy_preflight():
    print("\n[proxy preflight]")
    # Measured 2026-09-21: an exit whose password had been rotated answered
    # 407 in 0.2s to a plain request, while the SAME exit under Chromium gave
    # nothing but navigation timeouts — three 60s attempts per page, with the
    # proxy never mentioned. The family rule is that a proxy failure is not a
    # timeout; this is the case where the browser reports one anyway, so the
    # exit is checked before a browser is involved.
    eq("407 is credentials, not slowness",
       proxy_pool.classify_preflight("Tunnel connection failed: 407 Proxy "
                                     "Authentication Required"),
       proxy_pool.PREFLIGHT_REJECTED)
    eq("a refused tunnel is an unusable exit",
       proxy_pool.classify_preflight("ProxyError('Unable to connect to proxy', "
                                     "ConnectionRefusedError)"),
       proxy_pool.PREFLIGHT_UNUSABLE)
    eq("a read timeout is not blamed on the exit",
       proxy_pool.classify_preflight("Read timed out. (read timeout=15)"),
       proxy_pool.PREFLIGHT_SLOW)
    eq("and an unrecognised failure is not either",
       proxy_pool.classify_preflight("something nobody has seen yet"),
       proxy_pool.PREFLIGHT_SLOW)

    # A rejected exit ends the run as bad usage; a slow one only warns.
    pool = proxy_pool.ProxyPool(["http://user:pass@gate.example.com:9999"])
    real = proxy_pool.preflight
    try:
        proxy_pool.preflight = lambda url, target, timeout=15.0: (
            proxy_pool.PREFLIGHT_REJECTED, "407 Proxy Authentication Required")
        try:
            proxy_pool.check_exit_or_raise(pool, "https://www.screener.in/market/IN08/")
            check("a rejected exit stops the run", False)
        except proxy_pool.ProxyError as e:
            check("a rejected exit stops the run before a browser starts", True)
            check("and the message says it is the exit, not the site",
                  "not the site" in str(e))
            check("and carries no credentials", "pass@" not in str(e))
        proxy_pool.preflight = lambda url, target, timeout=15.0: (
            proxy_pool.PREFLIGHT_SLOW, "read timed out")
        proxy_pool.check_exit_or_raise(pool, "https://www.screener.in/market/IN08/")
        check("a slow exit only warns — the target may be what is slow", True)
        proxy_pool.preflight = lambda url, target, timeout=15.0: (
            proxy_pool.PREFLIGHT_OK, "HTTP 200")
        proxy_pool.check_exit_or_raise(pool, "https://www.screener.in/market/IN08/")
        check("a working exit says so and continues", True)
        proxy_pool.check_exit_or_raise(None, "https://www.screener.in/market/IN08/")
        check("and with no pool there is nothing to check", True)
    finally:
        proxy_pool.preflight = real

    for name in ("playwright_scraper", "puppeteer_scraper", "selenium_scraper"):
        source = open(os.path.join(REPO, f"{name}.py"), encoding="utf-8").read()
        check(f"{name} preflights its exit before launching a browser",
              "check_exit_or_raise(pool, args.url)" in source)




def check_captcha_solver_units():
    print("\n[captcha solver]")
    # NOT captures: screener.in configures no captcha, so the solver's own
    # mapping is exercised on minimal hand-built widgets with an obvious
    # placeholder sitekey. These test the solver, never the site.
    widget = ('<html><body><script src="https://challenges.cloudflare.com/turnstile/v0/api.js">'
              '</script><div class="cf-turnstile" data-sitekey="0xFIXTUREFIXTUREFIXTURE">'
              '</div></body></html>')
    found = captcha_solver.detect_in_html(widget, "https://www.screener.in/x")
    check("a Turnstile widget is detected", found is not None)
    if found:
        eq("and classified as Turnstile", found.kind, "turnstile")
        eq("and mapped to the documented task type",
           captcha_solver._task_for(found, 0.7)["type"], "TurnstileTaskProxyless")
    keyless = captcha_solver.detect_turnstile(widget.replace(
        ' data-sitekey="0xFIXTUREFIXTUREFIXTURE"', ""), "https://www.screener.in/x")
    check("a sitekey-less widget is reported, not ignored", keyless is not None)
    if keyless:
        try:
            captcha_solver.solve(keyless, "0" * 32)
            check("solving a sitekey-less challenge is refused", False)
        except RuntimeError as e:
            check("solving a sitekey-less challenge is refused with the reason, "
                  "before anything is paid for", "sitekey" in str(e))
    check("an arrow-function injector ships for Playwright/pyppeteer",
          captcha_solver.INJECT_TOKEN_FN.strip().startswith("(token) =>"))
    check("a function-body injector ships for Selenium",
          "arguments[0]" in captcha_solver.INJECT_TOKEN_BODY)
    v3 = captcha_solver.CaptchaChallenge(kind="recaptcha_v3", sitekey="6L" + "a" * 30,
                                         page_url="https://www.screener.in/x")
    eq("an out-of-range minScore is snapped to a documented one",
       captcha_solver._task_for(v3, 0.55)["minScore"], 0.7)

    # The capability sentence: never "cannot be solved", only "not implemented".
    docs = open(os.path.join(REPO, "captcha_solver.py"), encoding="utf-8").read()
    check("the solver's docstring names what it does NOT implement as a TODO",
          "does NOT implement" in docs and "RecaptchaV2EnterpriseTaskProxyless" in docs)
    for path in ("README.md", "captcha_solver.py", "page_flow.py"):
        full = os.path.join(REPO, path)
        if not os.path.exists(full):
            continue
        text = open(full, encoding="utf-8").read().lower()
        check(f"{path} never says a captcha cannot be solved",
              not re.search(r"(cannot|can't|can not) be solved|unsolvable captcha", text))


def check_solve_budget_is_enforced():
    print("\n[solve budget]")
    # A cap that only one of two call sites counted let a sibling's single
    # page buy three solves. Count the call sites, the guards and the
    # increments in every engine, and require them to line up.
    for name in ("playwright_scraper", "puppeteer_scraper", "selenium_scraper"):
        source = open(os.path.join(REPO, f"{name}.py"), encoding="utf-8").read()
        calls = len(re.findall(r"handle_captcha_if_present\([^)]*solves\)", source))
        guards = source.count("page_flow.solve_budget(solves[0])")
        increments = source.count("solves[0] += 1")
        solve_calls = len(re.findall(r"\btoken = solve\(", source))
        eq(f"{name}: one guard, one increment, one paid call", (guards, increments, solve_calls), (1, 1, 1))
        check(f"{name}: every captcha-handler call passes the page's counter",
              calls >= 1 and calls == source.count("handle_captcha_if_present(") - 1)
    check("the cap is one solve per page", page_flow.SOLVES_PER_PAGE == 1)
    check("the budget helper allows the first and refuses the second",
          page_flow.solve_budget(0) and not page_flow.solve_budget(1))


def check_output_contract():
    print("\n[output contract]")
    columns = list(asdict(Product()).keys())
    eq("the family prefix leads the schema, in order", columns[:7],
       ["source", "scraped_at", "url", "sku", "title", "price", "currency"])
    eq("then category, page and position", columns[7:10], ["category", "page", "position"])
    eq("source names the site", Product.source, "screener.in")
    for gone in ("brand", "rating", "original_price", "discount_pct", "price_source"):
        check(f"{gone!r} is not a column (no referent on a stock table)",
              gone not in columns)

    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        write_csv([], f"{prefix}.csv")
        with open(f"{prefix}.csv", newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        eq("an empty CSV still carries its header", header, columns)

        with open(f"{prefix}.json", "w", encoding="utf-8") as f:
            f.write('[{"sku": "yesterday"}]')
        with redirect_stdout(io.StringIO()):
            rc = save([], prefix, "json")
        eq("an empty run exits 4", rc, EXIT_NO_PRODUCTS)
        eq("and leaves last night's good output alone",
           json.load(open(f"{prefix}.json", encoding="utf-8")), [{"sku": "yesterday"}])
        with redirect_stdout(io.StringIO()):
            rc = save([], prefix, "json", allow_empty=True)
        eq("--allow-empty writes the empty result",
           json.load(open(f"{prefix}.json", encoding="utf-8")), [])

        os.remove(f"{prefix}.json")
        with redirect_stdout(io.StringIO()):
            rc = finish_run([], prefix, "json", False, blocked=True,
                            stop_reason="blocked_login_wall", pages_requested=1,
                            pages_completed=0, start_url="u", final_url="u")
        eq("a blocked run exits 3", rc, EXIT_BLOCKED)
        check("and writes no sidecar", not os.path.exists(f"{prefix}.meta.json"))

        # The family-wide unification of 2026-09-21: a run that never got its
        # content is exit 5, not "the listing is empty".
        for reason in ("page_load_timeout", "api_error", "page_not_found",
                       "content_unparsed", "page_never_painted",
                       "a_reason_nobody_has_invented_yet"):
            with redirect_stdout(io.StringIO()):
                rc = finish_run([], prefix, "json", False, blocked=False,
                                stop_reason=reason, pages_requested=1,
                                pages_completed=0, start_url="u", final_url="u")
            eq(f"0 rows because of {reason!r} is exit 5, not 4", rc, EXIT_FETCH_FAILED)
        with redirect_stdout(io.StringIO()):
            rc = finish_run([], prefix, "json", False, blocked=False,
                            stop_reason="no_results", pages_requested=1,
                            pages_completed=1, start_url="u", final_url="u")
        eq("while a listing the site says is empty IS exit 4", rc, EXIT_NO_PRODUCTS)

        # --allow-empty is for THAT case only. A blocked or failed run must
        # never replace good output with [], flag or no flag.
        for blocked, reason, want in ((True, "blocked_login_wall", EXIT_BLOCKED),
                                      (False, "page_load_timeout", EXIT_FETCH_FAILED),
                                      (False, "start_page_out_of_range", EXIT_FETCH_FAILED)):
            with open(f"{prefix}.json", "w", encoding="utf-8") as f:
                f.write('[{"sku": "yesterday"}]')
            if os.path.exists(f"{prefix}.meta.json"):
                os.remove(f"{prefix}.meta.json")
            with redirect_stdout(io.StringIO()):
                rc = finish_run([], prefix, "json", True, blocked=blocked,
                                stop_reason=reason, pages_requested=1,
                                pages_completed=0, start_url="u", final_url="u")
            eq(f"--allow-empty with {reason!r} still exits {want}", rc, want)
            eq(f"and leaves the previous good output alone ({reason})",
               json.load(open(f"{prefix}.json", encoding="utf-8")), [{"sku": "yesterday"}])
            check(f"and writes no sidecar ({reason})", not os.path.exists(f"{prefix}.meta.json"))

        class _O:
            lost = parse_failed = load_failed = raised = proxy_failed = False
        for st, want in ((page_flow.classify("<html></html>", status_code=404), "page_not_found"),
                         (page_flow.classify("<html></html>", status_code=500), "page_server_error")):
            o = _O(); o.state = st
            eq(f"a {st.state} page is named {want!r}, never 'blocked_…'",
               output_writer.failure_stop_reason(o), want)
        o = _O(); o.load_failed = o.proxy_failed = True; o.state = None
        eq("a dead exit is 'proxy_failed', not a timeout", output_writer.failure_stop_reason(o), "proxy_failed")
        o = _O(); o.raised = True; o.state = None
        eq("a fetch that raised is named as that", output_writer.failure_stop_reason(o), "page_fetch_raised")

        # A run that reached the end from ?page=3 is a TAIL, not the listing.
        with redirect_stdout(io.StringIO()):
            finish_run([_row(sku="3", rank=51)], prefix, "json", False, blocked=False,
                       stop_reason="listing_exhausted", pages_requested=5,
                       pages_completed=5, start_url="u", final_url="u", start_page=3)
        meta = json.load(open(f"{prefix}.meta.json", encoding="utf-8"))
        eq("reaching the end from page 3 is coverage 'tail'", (meta["coverage"], meta["start_page"]), ("tail", 3))

        with redirect_stdout(io.StringIO()):
            rc = finish_run([_row()], prefix, "json", False, blocked=False,
                            stop_reason="page_load_timeout", pages_requested=3,
                            pages_completed=1, pages_failed=[2],
                            start_url="u", final_url="u")
        eq("a run that gathered rows and then stopped exits 6", rc, EXIT_PARTIAL)
        meta = json.load(open(f"{prefix}.meta.json", encoding="utf-8"))
        eq("the sidecar names WHICH pages failed", meta["pages_failed"], [2])

        with redirect_stdout(io.StringIO()):
            rc = finish_run([_row(sku="1", rank=1), _row(sku="2", rank=4)], prefix,
                            "json", False, blocked=False,
                            stop_reason="listing_exhausted", pages_requested=9,
                            pages_completed=2, start_url="u", final_url="u",
                            total_results=42, pages_available=2, addressable=True)
        eq("running out of pages is a complete run", rc, 0)
        meta = json.load(open(f"{prefix}.meta.json", encoding="utf-8"))
        eq("and carries the site's own arithmetic",
           (meta["total_results"], meta["pages_available"]), (42, 2))
        eq("and the rank gap it could prove", meta["rank_gaps"], [[2, 3]])

    seen = set()
    rows = dedupe_by_sku([_row(sku="a"), _row(sku="b"), _row(sku="a")], seen)
    eq("duplicates are dropped", [r.sku for r in rows], ["a", "b"])
    rows = dedupe_by_sku([_row(sku=None), _row(sku=None)], set())
    eq("a row with no sku is never dropped as a duplicate", len(rows), 2)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "x.csv")
        write_csv([_row(extra_metrics={"Book value (Rs.)": 1.91})], path)
        with open(path, newline="", encoding="utf-8") as f:
            cell = next(csv.DictReader(f))["extra_metrics"]
        eq("extra_metrics goes into a CSV cell as JSON", json.loads(cell),
           {"Book value (Rs.)": 1.91})


# The flag contract every engine in the family exposes, as argparse
# destinations — plus `start_page`, which cli_types.finish_args derives from
# the URL rather than from a flag.
CONTRACT_FLAGS = {
    "url", "pages", "category", "format", "out", "delay", "retries",
    "retry_delay", "concurrency", "proxy", "proxy_file", "proxy_rotate",
    "proxy_shuffle", "proxy_block_retries", "twocaptcha_key", "captcha_api",
    "solve_captcha", "min_score", "cdp_endpoint", "allow_empty", "dump_html",
    "headless", "fingerprint", "fp_tags", "fp_country", "start_page",
}


def check_engine_parity():
    print("\n[engine parity]")
    if not ENGINES:
        skip("engine parity", "no engine library installed")
        return
    flag_sets = {}
    for name, module in ENGINES.items():
        # Through the real entry point, not by reading add_argument calls.
        flags = set(vars(module.parse_args(
            ["--url", "https://www.screener.in/market/IN08/"])))
        flag_sets[name] = flags
        missing, extra = CONTRACT_FLAGS - flags, flags - CONTRACT_FLAGS
        check(f"{name} carries every flag in the contract"
              + (f" (missing {sorted(missing)})" if missing else ""), not missing)
        check(f"{name} adds no flag outside the contract"
              + (f" (extra {sorted(extra)})" if extra else ""), not extra)
    if len(flag_sets) > 1:
        first = next(iter(flag_sets.values()))
        check("every engine exposes the SAME flags as its twins",
              all(s == first for s in flag_sets.values()))
    for name, module in ENGINES.items():
        eq(f"{name} uses the shared row selector", module.ITEM_CARD_SELECTOR,
           product_parser.SELECTORS["item_card"])
        check(f"{name}'s MIN_CARD_MATCHES is > 1", module.MIN_CARD_MATCHES > 1)
        source = inspect.getsource(module)
        check(f"{name}'s readiness wait treats reaching the minimum as success (>=)",
              "count >= minimum" in source and "count > MIN_CARD_MATCHES" not in source)
        check(f"{name} classifies with the SITE page it asked for",
              source.count("requested_page=page_num") >= 2)
    if len(ENGINES) > 1:
        eq("every engine agrees on MIN_CARD_MATCHES",
           len({m.MIN_CARD_MATCHES for m in ENGINES.values()}), 1)
    # Documented differences, asserted so closing one is a decision too.
    for name in ("puppeteer_scraper", "selenium_scraper"):
        source = open(os.path.join(REPO, f"{name}.py"), encoding="utf-8").read()
        check(f"{name} says --concurrency is not implemented rather than ignoring it",
              "--concurrency %d is not implemented" in source)
        check(f"{name} keeps a non-addressable page 2 in its outcomes, as the "
              f"primary engine does", "outcomes.remove(outcome)" not in source)
def check_readiness_wait_is_csp_safe():
    print("\n[CSP-safe readiness]")
    for name in ("playwright_scraper", "puppeteer_scraper", "selenium_scraper"):
        source = open(os.path.join(REPO, f"{name}.py"), encoding="utf-8").read()
        check(f"{name} does not wait on an evaluated string "
              f"(wait_for_function would break under a CSP without unsafe-eval)",
              not re.search(r"\.wait_for_function\s*\(", source))


def check_engine_imports_driver_at_module_level():
    print("\n[driver imports]")
    # An engine that imports its driver inside the launch path imports cleanly
    # with the driver absent: the offline suite's group never skips, and the
    # CI job that exists to fail on an unexpected skip cannot catch a broken
    # import. This drifts back silently, so it is asserted.
    expected = {"playwright_scraper": "playwright",
                "puppeteer_scraper": "pyppeteer",
                "selenium_scraper": "selenium"}
    for name, driver in expected.items():
        tree = ast.parse(open(os.path.join(REPO, f"{name}.py"), encoding="utf-8").read())
        top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        found = any(
            (isinstance(n, ast.ImportFrom) and (n.module or "").startswith(driver))
            or (isinstance(n, ast.Import) and any(a.name.startswith(driver) for a in n.names))
            for n in top_level)
        check(f"{name} imports {driver} at module level", found)


def _module_sources():
    for name in sorted(os.listdir(REPO)):
        if name.endswith(".py") and name != "smoke_test.py":
            yield name, open(os.path.join(REPO, name), encoding="utf-8").read()


def check_no_undefined_names():
    print("\n[undefined names]")
    # compileall proves a file PARSES, not that its names RESOLVE. A live run
    # elsewhere in this family died with NameError on a line reached only
    # while fetching, after an import had been removed — invisible to import,
    # --help, compileall and 400 green assertions. Deliberately coarse (one
    # pool of bindings, no scope tracking) so it under-reports rather than
    # inventing problems.
    import builtins
    for name, source in _module_sources():
        tree = ast.parse(source)
        bound = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    a = node.args
                    for arg in (a.posonlyargs + a.args + a.kwonlyargs
                                + ([a.vararg] if a.vararg else [])
                                + ([a.kwarg] if a.kwarg else [])):
                        bound.add(arg.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.Lambda):
                a = node.args
                for arg in (a.posonlyargs + a.args + a.kwonlyargs
                            + ([a.vararg] if a.vararg else [])
                            + ([a.kwarg] if a.kwarg else [])):
                    bound.add(arg.arg)
            elif isinstance(node, ast.comprehension):
                for target in ast.walk(node.target):
                    if isinstance(target, ast.Name):
                        bound.add(target.id)
            elif isinstance(node, ast.Global):
                bound.update(node.names)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unknown = sorted(used - bound)
        check(f"{name}: every name it loads is imported, defined or assigned"
              + (f" (unknown: {unknown})" if unknown else ""), not unknown)




def check_shared_calls_bind():
    print("\n[shared call signatures]")
    # The check that found six broken call sites in one pass on a sibling:
    # `classify(html, status, url)` took `status` positionally while two of
    # three engines called it `classify(html, url=...)`, and both crashed on
    # their FIRST fetch.
    #
    # Two rules a sibling's version of this check got wrong, both fixed here:
    #   * a name the shared module does NOT define is reported, not skipped —
    #     `getattr(..., None)` followed by "skip if not callable" swallowed
    #     the loudest failure the check could report;
    #   * a name bound anywhere in the calling file shadows a same-named
    #     module (an engine's `proxy_pool` parameter is a ProxyPool, not the
    #     module), or the check reports false positives by the dozen.
    shared_modules = {"product_parser": product_parser, "page_flow": page_flow,
                      "output_writer": output_writer, "captcha_solver": captcha_solver,
                      "proxy_pool": proxy_pool, "env_config": env_config,
                      "cli_types": cli_types}
    placeholder = object()
    bound_calls = 0
    for name, source in _module_sources():
        tree = ast.parse(source)
        callables, modules, problems = {}, {}, []
        locally_bound = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                locally_bound.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                locally_bound.add(node.id)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in shared_modules:
                for alias in node.names:
                    if not hasattr(shared_modules[node.module], alias.name):
                        problems.append(f"imports {alias.name!r}, which "
                                        f"{node.module} does not define")
                        continue
                    target = getattr(shared_modules[node.module], alias.name)
                    if callable(target):
                        callables[alias.asname or alias.name] = target
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name in shared_modules and local not in locally_bound:
                        modules[local] = shared_modules[alias.name]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if isinstance(node.func, ast.Name):
                target = callables.get(node.func.id)
            elif (isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id in modules):
                module = modules[node.func.value.id]
                if not hasattr(module, node.func.attr):
                    problems.append(f"line {node.lineno}: {node.func.value.id}."
                                    f"{node.func.attr} does not exist")
                    continue
                target = getattr(module, node.func.attr)
                if not callable(target):
                    problems.append(f"line {node.lineno}: {node.func.value.id}."
                                    f"{node.func.attr} is not callable")
                    continue
            if target is None or isinstance(target, type):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords):
                continue
            try:
                signature = inspect.signature(target)
            except (TypeError, ValueError):
                continue
            try:
                signature.bind(*([placeholder] * len(node.args)),
                               **{k.arg: placeholder for k in node.keywords})
                bound_calls += 1
            except TypeError as e:
                problems.append(f"line {node.lineno}: "
                                f"{getattr(target, '__name__', target)} {e}")
        check(f"{name}: every call into a shared module binds against its real "
              f"signature" + (f" — {problems}" if problems else ""), not problems)
    # A binding check that binds nothing passes for the wrong reason.
    check(f"the binding check actually bound calls ({bound_calls})", bound_calls > 50)


def check_no_code_after_a_terminator():
    print("\n[unreachable code]")
    # Found the same fifteen dead lines in six sibling repos, byte for byte: a
    # function whose `def` had been lost, its body absorbed into the end of
    # the function above, after a `return`. It parses, imports and compiles.
    hits, scanned = [], 0
    for name, source in _module_sources():
        scanned += 1
        for node in ast.walk(ast.parse(source)):
            for field in ("body", "orelse", "finalbody", "handlers"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                        hits.append(f"{name}:{block[i + 1].lineno}")
    check(f"no statement follows a return/raise/break/continue in its block "
          f"({scanned} modules)" + (f" — {hits}" if hits else ""),
          not hits and scanned > 5)


def _in_virtualenv(dirpath):
    """A directory holding pyvenv.cfg is a virtualenv, whatever it is called."""
    return os.path.exists(os.path.join(dirpath, "pyvenv.cfg"))


def check_banned_wording():
    print("\n[wording]")
    # Assembled from pieces, so this file can be scanned like every other
    # instead of being exempted — the file most likely to acquire a stray
    # phrase is the suite itself.
    banned = ["anti" + "detect", "cloud " + "browser", "gate." + "2prx.com",
              "ANTI" + "DETECT_LOCAL_API"]
    scanned, hits = 0, []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs
                   if d not in {".git", "__pycache__", "captures", "legacy", "live"}
                   and not _in_virtualenv(os.path.join(root, d))]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".example", ".toml", ".cfg")) \
                    and filename not in ("Dockerfile", ".gitignore", ".dockerignore"):
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read().lower()
            scanned += 1
            for phrase in banned:
                if phrase.lower() in text:
                    hits.append(f"{os.path.relpath(path, REPO)}: {phrase!r}")
    check(f"no shipped file uses a banned product name ({scanned} files scanned, "
          f"this suite included)" + (f" — {hits}" if hits else ""),
          not hits and scanned > 20)
    readme = os.path.join(REPO, "README.md")
    if os.path.exists(readme):
        check("the README names the Scraping Browser API",
              "Scraping Browser API" in open(readme, encoding="utf-8").read())




def check_removed_flags_stay_removed():
    print("\n[removed flags]")
    # Scoped to the ENGINES: --country is banned on a scraper (it could
    # disagree with the URL) and legitimate on fingerprint_client.py, where it
    # picks a fingerprint's locale.
    for name in ("playwright_scraper", "puppeteer_scraper", "selenium_scraper",
                 "scraper_api_client"):
        source = open(os.path.join(REPO, f"{name}.py"), encoding="utf-8").read()
        check(f"{name} does not reintroduce --country",
              '"--country"' not in source)
        check(f"{name} does not reintroduce the removed local-solver flag",
              '"--' + "anti" + "detect" + '"' not in source)
        # screener.in ignores ?limit= for an anonymous visitor (measured:
        # 10 and 50 both return 25 rows), so a --limit flag would be a
        # setting that looks configurable and is not.
        check(f"{name} does not reintroduce --limit",
              '"--limit"' not in source)
    fingerprint = open(os.path.join(REPO, "fingerprint_client.py"), encoding="utf-8").read()
    check("fingerprint_client.py DOES still have --country (it picks a "
          "fingerprint's locale, which is a different thing)",
          '"--country"' in fingerprint)
    check("and defaults --fp-tags to ONE OS-family tag, which is what the API "
          "accepts", all('"--fp-tags", default="Windows"' in
                         open(os.path.join(REPO, f"{n}.py"), encoding="utf-8").read()
                         for n in ("playwright_scraper", "puppeteer_scraper",
                                   "selenium_scraper")))




def check_env_example_matches_code():
    print("\n[.env.example]")
    example_path = os.path.join(REPO, ".env.example")
    if not os.path.exists(example_path):
        check(".env.example exists", False)
        return
    documented = set(env_config.documented_keys(example_path))
    known = set(env_config.ENV_KEYS)
    eq("every documented variable is read by the code", documented - known, set())
    eq("every variable the code reads is documented", known - documented, set())

    # Round-trip a COPIED example through the real loader: every credential
    # must read as unset. The braced placeholders 2Captcha's own docs use
    # ({login}, {password}) once sailed through a literal-only check and
    # produced a 401 a long way from its cause.
    saved = {k: os.environ.get(k) for k in known}
    try:
        for line in open(example_path, encoding="utf-8"):
            parsed = env_config._parse_line(line)
            if parsed:
                os.environ[parsed[0]] = parsed[1]
        credentials = {k for k in known
                       if "KEY" in k or "PROXY" in k or "CDP" in k}
        for key in sorted(credentials):
            eq(f"{key} from a copied .env.example reads as unset",
               env_config.env_value(key), None)
        # The other half of the same check: a NON-credential default must
        # survive being copied, or the example is useless. SCREENER_URL is a
        # real listing URL on purpose.
        for key in sorted(known - credentials):
            check(f"{key} from a copied .env.example is still usable",
                  bool(env_config.env_value(key)))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    check("an empty value is unset and is NOT warned about "
          "(an unset CI secret arrives empty)", env_config.is_placeholder(""))
    check("a braced vendor example is treated as unset",
          env_config.is_placeholder("ws://{login}-zone-x:{password}@cb.2captcha.com:9222"))
    check("a real credentialled URL is NOT treated as a placeholder",
          not env_config.is_placeholder("ws://real-login:" + "realpass@cb.2captcha.com:9222"))


def check_env_precedence():
    print("\n[.env precedence]")
    class Args:
        twocaptcha_key = None
        url = None
    saved = os.environ.get("TWOCAPTCHA_KEY")
    try:
        os.environ["TWOCAPTCHA_KEY"] = "from-the-environment"
        args = Args()
        env_config.apply(args, keys={"TWOCAPTCHA_KEY": "twocaptcha_key"}, quiet=True)
        eq("an unset flag is filled from the environment",
           args.twocaptcha_key, "from-the-environment")
        args = Args()
        args.twocaptcha_key = "typed-on-the-command-line"
        env_config.apply(args, keys={"TWOCAPTCHA_KEY": "twocaptcha_key"}, quiet=True)
        eq("an explicit flag always wins", args.twocaptcha_key,
           "typed-on-the-command-line")
    finally:
        if saved is None:
            os.environ.pop("TWOCAPTCHA_KEY", None)
        else:
            os.environ["TWOCAPTCHA_KEY"] = saved


def check_policy_constants_have_consumers():
    print("\n[policy constants]")
    # A policy constant nothing reads is the same defect as dead code, and
    # harder to see, because the prose around it reads like enforcement
    #.
    # A consumer may reach the constant through an accessor its own module
    # exposes — `state.policy` reads STATE_POLICY — so each entry names the
    # spellings that count as reading it. What is NOT allowed is a constant
    # with a paragraph of justification and no reader at all, or a second
    # copy of its values somewhere else (which is how `--proxy-rotate`'s
    # choices drifted away from ROTATE_MODES).
    constants = {
        "STATE_POLICY": ("page_flow.py", ("STATE_POLICY", ".policy")),
        "MIN_ASSET_REFERENCES": ("product_parser.py", ("MIN_ASSET_REFERENCES",)),
        "BOT_CHALLENGE_MARKERS": ("product_parser.py",
                                  ("BOT_CHALLENGE_MARKERS", "detect_bot_challenge")),
        "COMPLETE_STOP_REASONS": ("output_writer.py",
                                  ("COMPLETE_STOP_REASONS", "finish_run")),
        "ROTATE_MODES": ("proxy_pool.py", ("ROTATE_MODES",)),
        "ENV_KEYS": ("env_config.py", ("ENV_KEYS", "env_config.apply")),
        "REFUSAL_STATUSES": ("page_flow.py", ("page_flow.classify",)),
        "SOLVES_PER_PAGE": ("page_flow.py", ("solve_budget", "SOLVES_PER_PAGE")),
        "FIXED_COLUMNS": ("product_parser.py", ("parse_products",)),
        "PAGE_SIZE": ("product_parser.py", ("page_flow.shortfall", "PAGE_SIZE")),
    }
    for constant, (home, spellings) in constants.items():
        consumers = [name for name, source in _module_sources()
                     if name != home and any(sp in source for sp in spellings)]
        check(f"{constant} is read outside {home}"
              + (f" (by {consumers})" if consumers else " — NOTHING reads it"),
              bool(consumers))
    # The values themselves must not be copied: an engine spelling out
    # ["per-run", "per-page"] would look correct and drift silently.
    copies = [name for name, source in _module_sources()
              if name != "proxy_pool.py" and '"per-run", "per-page"' in source]
    check("no module re-spells ROTATE_MODES' values instead of importing them"
          + (f" ({copies} does)" if copies else ""), not copies)




def check_dockerfile_copies_what_it_imports():
    print("\n[Dockerfile]")
    path = os.path.join(REPO, "Dockerfile")
    if not os.path.exists(path):
        check("Dockerfile exists", False)
        return
    dockerfile = open(path, encoding="utf-8").read()
    # Join line continuations first: a multi-line COPY is the normal shape
    # here, and reading only its first line would let this check pass while
    # the image was missing everything after the first backslash.
    joined = re.sub(r"\\\s*\n", " ", dockerfile)
    copied = set()
    for line in re.findall(r"^COPY\s+(.+)$", joined, re.MULTILINE):
        for token in line.split():
            if token.endswith(".py"):
                copied.add(token)
    # The entrypoint's transitive local imports — the image should carry
    # exactly these. All three repos in this family once shipped an image that
    # died with ModuleNotFoundError on every invocation, --help included,
    # because one module was missing from this list, and CI never built it.
    local = {n[:-3] for n, _ in _module_sources()}
    needed, queue = set(), ["playwright_scraper"]
    while queue:
        module = queue.pop()
        if module in needed:
            continue
        needed.add(module)
        source = open(os.path.join(REPO, f"{module}.py"), encoding="utf-8").read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in local:
                queue.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in local:
                        queue.append(alias.name)
    missing = {f"{m}.py" for m in needed} - copied
    check("the Dockerfile COPY list carries every module the entrypoint "
          "imports" + (f" (missing {sorted(missing)})" if missing else ""),
          not missing)
    check("and carries no test suite or fixtures",
          "smoke_test.py" not in copied and "captures" not in dockerfile)
    check("and no .env is baked into the image",
          not re.search(r"^COPY\s+.*\.env(\s|$)", dockerfile, re.MULTILINE))




def check_sample_output():
    print("\n[sample output]")
    columns = list(asdict(Product()).keys())
    json_path = os.path.join(REPO, "sample_output.json")
    csv_path = os.path.join(REPO, "sample_output.csv")
    if not (os.path.exists(json_path) and os.path.exists(csv_path)):
        check("sample_output.json and sample_output.csv are committed", False)
        return
    rows = json.load(open(json_path, encoding="utf-8"))
    check("the sample holds rows from a real run", bool(rows))
    eq("its columns match the Product schema", list(rows[0].keys()), columns)
    with open(csv_path, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    eq("the CSV header matches too", header, columns)
    blob = json.dumps(rows).lower()
    for marker in ("lorem ipsum", "example.com", "your_api_key", "acme"):
        check(f"the sample is not fabricated ({marker!r} absent)", marker not in blob)
    check("every sample row carries the site's own id and a company URL",
          all(r.get("sku") and "/company/" in (r.get("url") or "") for r in rows))




def check_oldest_supported_python_can_parse_it():
    print("\n[Python floor]")
    # pyproject.toml and the CI matrix both claim 3.9. Claiming a floor
    # without testing it is how a walrus operator or an `X | None` annotation
    # ships and breaks it for everyone on that version. This
    # parses every module under 3.9's grammar; CI additionally RUNS the suite
    # on 3.9, which is the half this cannot do.
    for name, source in _module_sources():
        try:
            ast.parse(source, filename=name, feature_version=(3, 9))
            ok, why = True, ""
        except SyntaxError as e:
            ok, why = False, f" — {e}"
        check(f"{name} parses under Python 3.9's grammar{why}", ok)
    for extra in ("smoke_test.py", os.path.join("tests", "test_smoke.py"),
                  os.path.join(".github", "ci_checks.py")):
        path = os.path.join(REPO, extra)
        if not os.path.exists(path):
            continue
        try:
            ast.parse(open(path, encoding="utf-8").read(), filename=extra,
                      feature_version=(3, 9))
            ok = True
        except SyntaxError as e:
            ok = False
            print(f"        {e}")
        check(f"{extra} parses under Python 3.9's grammar", ok)


def check_packaging_matches_the_tree():
    print("\n[packaging]")
    path = os.path.join(REPO, "pyproject.toml")
    if not os.path.exists(path):
        check("pyproject.toml exists", False)
        return
    try:
        import tomllib
    except ImportError:
        skip("pyproject.toml cross-check", "tomllib needs 3.11; CI's newest leg runs it")
        return
    with open(path, "rb") as handle:
        config = tomllib.load(handle)
    declared = set(config["tool"]["setuptools"]["py-modules"])
    on_disk = {name[:-3] for name, _ in _module_sources()}
    eq("every module on disk is declared in pyproject", on_disk - declared, set())
    eq("and nothing declared is missing from disk", declared - on_disk, set())
    # requirements.txt and the dependency list are kept in sync BY HAND, and
    # CI installs only requirements.txt — so drift here goes unnoticed there.
    requirements = [line.split("#")[0].strip()
                    for line in open(os.path.join(REPO, "requirements.txt"),
                                     encoding="utf-8")
                    if line.strip() and not line.strip().startswith("#")]
    eq("pyproject's dependencies match requirements.txt",
       sorted(config["project"]["dependencies"]), sorted(requirements))
    for engine in ("playwright", "puppeteer", "selenium"):
        extra = config["project"]["optional-dependencies"][engine]
        pins = [line.split("#")[0].strip()
                for line in open(os.path.join(REPO, f"requirements-{engine}.txt"),
                                 encoding="utf-8")
                if line.strip() and not line.strip().startswith("#")]
        eq(f"the {engine} extra matches requirements-{engine}.txt",
           sorted(extra), sorted(pins))


def check_ci_checks_are_one_implementation():
    print("\n[CI checks]")
    # ONE implementation, invoked from here AND from the workflow. The older
    # repos in this family carried this script plus a narrower inline grep in
    # tests.yml, and the two disagreed: the inline one matched only ws:// and
    # wss://, so an http://user:pass@ credential would have sailed past CI,
    # while the script itself failed on its own main branch.
    path = os.path.join(REPO, ".github", "ci_checks.py")
    if not os.path.exists(path):
        check(".github/ci_checks.py exists", False)
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("ci_checks", path)
    ci_checks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ci_checks)

    failures = ci_checks.secret_check()
    check("nothing credential-shaped, no session material and no personal data "
          "is committed" + (f" — {failures[:3]}" if failures else ""), not failures)
    failures = ci_checks.sample_check()
    check("the committed sample is real and matches the schema"
          + (f" — {failures[:3]}" if failures else ""), not failures)

    # And the CLI itself, not only the functions underneath it: a signature
    # or a call that drifts inside main() is invisible to a test that only
    # exercises the internals, and CI invokes exactly this command line.
    import subprocess
    result = subprocess.run([sys.executable, path, "--all"],
                            capture_output=True, text=True, cwd=REPO)
    check("`python3 .github/ci_checks.py --all` exits 0"
          + ("" if result.returncode == 0 else
             f" — {(result.stdout + result.stderr).strip().splitlines()[-1][:120]}"),
          result.returncode == 0)

    workflow = os.path.join(REPO, ".github", "workflows", "tests.yml")
    if os.path.exists(workflow):
        text = open(workflow, encoding="utf-8").read()
        check("the workflow CALLS ci_checks.py rather than reimplementing it",
              "ci_checks.py" in text)
        check("and carries no inline credential grep of its own",
              not re.search(r"grep\s+-[a-zA-Z]*\s*['\"]?\(ws\|wss\)", text))
    else:
        check(".github/workflows/tests.yml exists", False)


# ---------------------------------------------------------------------------
# 6. The concurrency machinery, with the browser stubbed out
# ---------------------------------------------------------------------------
class _FakeResponse:
    """Just enough of a `requests` response for the paid paths to run."""

    def __init__(self, payload, status=200, headers=None):
        self._payload, self.status_code = payload, status
        self.headers = headers or {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeResponse:
    """Just enough of a `requests` response for the paid paths to run."""

    def __init__(self, payload, status=200, headers=None):
        self._payload, self.status_code = payload, status
        self.headers = headers or {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def check_credentialled_paths_run():
    print("\n[paths a credential gates]")
    # The paths nobody runs are the paths with no evidence behind them, and in
    # this family they are where the copied core rotted: a key printed to a
    # terminal, a user agent never applied, a documented flag that returns 400.
    # This repo has no 2Captcha key to test with, so the HTTP call is stubbed
    # and everything around it is executed for real — which is what catches a
    # name that does not resolve, a signature that drifted, or a response key
    # that is read but never returned.
    import requests
    import captcha_solver as cs
    import fingerprint_client as fc
    import scraper_api_client as sac

    calls = []
    params = []
    sent_payloads = []

    def fake_post(url, **kwargs):
        calls.append(url)
        if url.endswith("/createTask"):
            return _FakeResponse({"errorId": 0, "taskId": "T1"})
        if url.endswith("/getTaskResult"):
            return _FakeResponse({"errorId": 0, "status": "ready",
                                  "solution": {"token": "TOKEN-V2"}})
        if url.endswith("/getBalance"):
            return _FakeResponse({"errorId": 0, "balance": "12.5"})
        if "in.php" in url:
            return _FakeResponse({"status": 1, "request": "ID1"})
        if "tasks/sync" in url:
            target = kwargs["json"]["url"]
            sent_payloads.append(kwargs["json"])
            name = {"https://www.screener.in/market/IN08/IN0801/IN080101/": "market_p1",
                    "https://www.screener.in/market/IN08/IN0801/IN080101/?page=2": "market_p2"}[target]
            # The API's real shape: `status` is a verdict STRING and the
            # target's code is `http_code`. And the x-debug header echoes the
            # task, credentialled cdpurl included.
            return _FakeResponse({"status": "success", "http_code": 200,
                                  "body": fx(name)[0]},
                                 headers={"x-debug": json.dumps(
                                     {"price": 0.0005, "cdpurl":
                                      "ws://acct-zone-x:" + "hunter2secret@cb.2captcha.com:9222"})})
        raise AssertionError(url)

    def fake_get(url, **kwargs):
        calls.append(url)
        params.append(kwargs.get("params") or {})
        if "res.php" in url:
            return _FakeResponse({"status": 1, "request": "TOKEN-V1"})
        if "fingerprint" in url:
            return _FakeResponse({
                "id": "fp1", "country": "de", "userAgent": {"value": "UA/9"},
                "screen": {"width": 1920, "height": 1080}, "locale": "de-DE",
                "timezone": "Europe/Berlin",
                "navigator": {"platform": "Win32", "hardwareConcurrency": 8},
                "webgl": {"vendor": "V", "renderer": "R"}})
        raise AssertionError(url)

    real_post, real_get, real_sleep = requests.post, requests.get, cs.time.sleep
    try:
        requests.post, requests.get, cs.time.sleep = fake_post, fake_get, lambda s: None
        turnstile = cs.CaptchaChallenge(kind="turnstile", sitekey="0xFIXTUREFIXTURE",
                                        page_url="https://www.screener.in/x")
        eq("the v2 solver returns its token", cs.solve(turnstile, "k" * 32), "TOKEN-V2")
        eq("the v1 fallback returns its token",
           cs.solve(turnstile, "k" * 32, api_version="v1"), "TOKEN-V1")
        v3 = cs.CaptchaChallenge(kind="recaptcha_v3", sitekey="6L" + "a" * 30,
                                 page_url="https://www.screener.in/x")
        eq("and both speak reCAPTCHA too", cs.solve(v3, "k" * 32), "TOKEN-V2")
        eq("the balance check parses its answer", cs.get_balance("k" * 32), 12.5)
        check("the key never rides in a URL on the v2 path",
              all("key=" not in c for c in calls if "api.2captcha.com" in c))

        fp = fc.get_fingerprint("k" * 32, tags="Windows,Chrome,Desktop", cache_dir=None)
        kwargs = fc.playwright_context_kwargs(fp)
        eq("a fingerprint's user agent is actually applied",
           kwargs.get("user_agent"), "UA/9")
        eq("its locale comes from the response, not from the country",
           kwargs.get("locale"), "de-DE")
        eq("and its timezone is applied at all", kwargs.get("timezone_id"),
           "Europe/Berlin")
        # The defect this pins: every engine in the family shipped
        # --fp-tags "Windows,Chrome,Desktop", and the API answers 400 to a
        # list. A caller that passes one anyway gets it trimmed, and says so.
        sent = [p.get("tags") for p in params if "tags" in p]
        eq("a three-tag --fp-tags is trimmed to the one tag the API accepts",
           sent, ["Windows"])
        # The key DOES ride in this endpoint's query string — that is the
        # API's shape, not a choice — which is why the client wraps the call
        # and redacts before re-raising. Asserted in check_credentials_never_leak.
        check("the fingerprint call is the one that carries its key in the URL",
              any("key" in p for p in params))

        with tempfile.TemporaryDirectory() as tmp:
            class Args:
                url = "https://www.screener.in/market/IN08/IN0801/IN080101/"
                key = "k" * 32
                timeout = 60
                cdp_url = "ws://acct-zone-x:" + "hunter2secret@cb.2captcha.com:9222"
                wait_text = None
                wait_element = "tr[data-row-company-id]"
                wait_state = None
                pages = 2
                start_page = 1
                dump_html = None
                out = os.path.join(tmp, "api_run")
                category = None
                format = "json"
                allow_empty = False
                retries = 0
                retry_delay = 0
                delay = 0

            logs = io.StringIO()
            handler = logging.StreamHandler(logs)
            sac.logger.addHandler(handler)
            level = sac.logger.level
            sac.logger.setLevel(logging.INFO)
            try:
                with redirect_stdout(io.StringIO()):
                    rc = sac.scrape(Args())
            finally:
                sac.logger.removeHandler(handler)
                sac.logger.setLevel(level)
            eq("the browserless engine parses real responses and exits 0", rc, 0)
            rows = json.load(open(f"{os.path.join(tmp, 'api_run')}.json", encoding="utf-8"))
            eq("with page 1 and page 2's kept rows", len(rows), 8)
            check("waitFor is sent as an OBJECT, not a JSON string",
                  all(isinstance(p.get("waitFor"), dict) for p in sent_payloads))
            text = logs.getvalue()
            check("the x-debug header IS logged (it is where the cost shows up)",
                  "x-debug" in text and "0.0005" in text)
            check("and the credentialled cdpurl inside it is not",
                  "hunter2secret" not in text)
    finally:
        requests.post, requests.get, cs.time.sleep = real_post, real_get, real_sleep




def check_concurrency_machinery():
    print("\n[concurrency]")
    engine = ENGINES.get("playwright_scraper")
    if engine is None:
        skip("concurrency machinery", "playwright not installed")
        return

    # A live run cannot always reach this code: pages 1 and 2 are fetched
    # alone and decide whether the rest may be addressed, so a blocked page 1
    # means the workers never start.
    class _StubSession:
        def __init__(self, *a, **kw):
            self.pool = None

        def open(self):
            return self

        def close(self):
            pass

    class _StubPlaywright:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Args:
        pages = 12
        delay = 0
        out = "stub"
        retries = 1

    original = (engine.sync_playwright, engine._BrowserSession, engine._fetch_one_page)
    fetched = []
    lock = __import__("threading").Lock()
    try:
        engine.sync_playwright = lambda: _StubPlaywright()
        engine._BrowserSession = _StubSession

        def fake_fetch(session, args, pool, page_num, url):
            with lock:
                fetched.append(page_num)
            outcome = engine.PageOutcome(page_num=page_num, url=url)
            outcome.state = page_flow.PageState(page_flow.CONTENT, "stub")
            outcome.products = [_row(sku=f"p{page_num}-{i}") for i in range(3)]
            return outcome

        engine._fetch_one_page = fake_fetch
        specs = [(n, f"u{n}") for n in range(3, 13)]
        results, unattempted, ran_out, empty_at = engine._fetch_pages_concurrently(
            Args(), None, specs, 4)
        check("no page reports an empty page when none was empty", empty_at is None)
        eq("every queued page is fetched exactly once",
           sorted(fetched), [n for n, _ in specs])
        eq("nothing is left unattempted when all pages succeed", unattempted, [])
        eq("outcomes are restorable to page order",
           [o.page_num for o in sorted(results, key=lambda o: o.page_num)],
           [n for n, _ in specs])

        # A page with no listings ends the listing and stops dispatch, so
        # asking for 50 pages of a 5-page search costs at most N-1 extra.
        fetched.clear()

        def fetch_until_empty(session, args, pool, page_num, url):
            with lock:
                fetched.append(page_num)
            outcome = engine.PageOutcome(page_num=page_num, url=url)
            outcome.state = page_flow.PageState(page_flow.CONTENT, "stub")
            outcome.products = [] if page_num >= 5 else [_row(sku=f"p{page_num}")]
            return outcome

        engine._fetch_one_page = fetch_until_empty
        results, unattempted, ran_out, empty_at = engine._fetch_pages_concurrently(
            Args(), None, specs, 2)
        check("the end of the listing stops dispatch", ran_out)
        eq("and the page that ended it is named, so the caller can tell a "
           "legitimate stop from a gap below it", empty_at, 5)
        check("so most of the queue is never fetched "
              f"({len(fetched)} fetched of {len(specs)} queued)",
              len(fetched) < len(specs))
        check("and the unattempted pages are REPORTED, not counted as failed",
              sorted(unattempted) == sorted(n for n, _ in specs if n not in fetched))

        # A worker that raises must neither hang the run nor lose its
        # siblings' pages.
        fetched.clear()

        def sometimes_explodes(session, args, pool, page_num, url):
            with lock:
                fetched.append(page_num)
            if page_num == 4:
                raise RuntimeError("stub worker failure")
            outcome = engine.PageOutcome(page_num=page_num, url=url)
            outcome.state = page_flow.PageState(page_flow.CONTENT, "stub")
            outcome.products = [_row(sku=f"p{page_num}")]
            return outcome

        engine._fetch_one_page = sometimes_explodes
        results, unattempted, ran_out, empty_at = engine._fetch_pages_concurrently(
            Args(), None, specs, 3)
        check("and its siblings' pages still come back", len(results) >= 1)

        # The regression this group exists for. The page that raised was
        # already off the queue, so before the fix it appeared in NO list —
        # not results, not unattempted, not pages_failed — and the run went on
        # to report itself complete with that page's listings missing.
        returned = {o.page_num for o in results}
        check("the page whose fetch raised is reported, not lost",
              4 in returned)
        lost = [o for o in results if o.page_num == 4][0]
        check("and it is reported as a page that yielded nothing",
              not lost.ok)
        eq("with a stop reason that names what happened",
           output_writer.failure_stop_reason(lost), "worker_lost_page")
        eq("every requested page is in exactly one of results/unattempted",
           sorted(returned | set(unattempted)), [n for n, _ in specs])

        # A worker can also die BEFORE the per-page handler above can run —
        # between taking a page off the queue and the fetch itself. The
        # reconciliation pass is the backstop for that, and it is tested
        # through a real failure rather than a stubbed one: the inter-page
        # pause is the code that sits in that gap, so a delay it cannot sleep
        # on kills the worker exactly there.
        fetched.clear()

        class HostileDelay:
            """Unusable as a number, so time.sleep(args.delay) raises."""

        class ArgsBadDelay(Args):
            delay = HostileDelay()

        engine._fetch_one_page = fake_fetch
        results, unattempted, ran_out, empty_at = (
            engine._fetch_pages_concurrently(ArgsBadDelay(), None, specs, 1))
        taken = sorted({o.page_num for o in results} | set(unattempted))
        eq("a worker dying between the queue and the fetch still leaves every "
           "page accounted for", taken, [n for n, _ in specs])
        reconciled = [o for o in results if o.lost]
        check("and the page it was holding is reconciled back as lost",
              len(reconciled) == 1 and reconciled[0].page_num == 4)
    finally:
        engine.sync_playwright, engine._BrowserSession, engine._fetch_one_page = original


def check_worker_pools_start_on_different_exits():
    print("\n[worker pools]")
    engine = ENGINES.get("playwright_scraper")
    if engine is None:
        skip("worker pools", "playwright not installed")
        return
    pool = proxy_pool.ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
    starts = [engine._worker_pool(pool, i).current for i in range(3)]
    eq("each worker starts on a different exit", len(set(starts)), 3)
    check("and holds its own pool object, so no thread needs a lock",
          engine._worker_pool(pool, 0) is not engine._worker_pool(pool, 0))
    eq("with no pool there is nothing to hand out",
       engine._worker_pool(None, 0), None)





def check_run_integrity():
    """No page may go missing, and no window may pass for a whole listing.

    Every check here is a case that used to exit 0 with status=complete while
    the output was short of what it claimed.
    """
    print("\n[run integrity]")
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "run")

        # 1. A page that went missing without anything noticing. stop_reason
        #    still says "completed" because the engine never saw it go.
        with redirect_stdout(io.StringIO()):
            rc = finish_run([_row()], prefix, "json", False, blocked=False,
                            stop_reason="completed", pages_requested=6,
                            pages_completed=5, start_url="u", final_url="u")
        eq("fewer pages back than asked for cannot be a complete run",
           rc, EXIT_PARTIAL)
        meta = json.load(open(f"{prefix}.meta.json", encoding="utf-8"))
        eq("the sidecar says partial", meta["status"], "partial")
        eq("and the stop reason names the gap", meta["stop_reason"],
           "pages_missing")

        with redirect_stdout(io.StringIO()):
            rc = finish_run([_row()], prefix, "json", False, blocked=False,
                            stop_reason="completed", pages_requested=6,
                            pages_completed=6, pages_missing=[4],
                            start_url="u", final_url="u")
        eq("a page reported missing is partial even when the count adds up",
           rc, EXIT_PARTIAL)
        meta = json.load(open(f"{prefix}.meta.json", encoding="utf-8"))
        eq("and the sidecar names which page", meta["pages_missing"], [4])

        # 2. coverage: what the run went for, beside whether it got it.
        with redirect_stdout(io.StringIO()):
            finish_run([_row()], prefix, "json", False, blocked=False,
                       stop_reason="completed", pages_requested=3,
                       pages_completed=3, start_url="u", final_url="u")
        meta = json.load(open(f"{prefix}.meta.json", encoding="utf-8"))
        eq("three pages of a longer listing is a complete WINDOW",
           (meta["status"], meta["coverage"]), ("complete", "window"))
        with redirect_stdout(io.StringIO()):
            finish_run([_row()], prefix, "json", False, blocked=False,
                       stop_reason="listing_exhausted", pages_requested=9,
                       pages_completed=4, start_url="u", final_url="u")
        meta = json.load(open(f"{prefix}.meta.json", encoding="utf-8"))
        eq("reaching the end of the listing is exhaustive coverage",
           (meta["status"], meta["coverage"]), ("complete", "exhaustive"))

        # 3. A page that carried listings and parsed to nothing is a parse
        #    failure, never the end of the listing.
        for reason in ("content_unparsed", "page_never_painted",
                       "worker_lost_page", "pages_unattempted", "pages_missing"):
            check(f"{reason} is not a complete stop reason",
                  reason not in output_writer.COMPLETE_STOP_REASONS)

        class _Outcome:
            def __init__(self, state):
                self.state = state
                self.parse_failed = True
                self.load_failed = False
                self.lost = False

        eq("a page full of listings that would not parse is a schema failure",
           output_writer.failure_stop_reason(
               _Outcome(page_flow.PageState(page_flow.CONTENT, "25 rows"))),
           "content_unparsed")
        eq("a page that never painted is named as that instead",
           output_writer.failure_stop_reason(
               _Outcome(page_flow.PageState(page_flow.UNPAINTED, "no table yet"))),
           "page_never_painted")
        with redirect_stdout(io.StringIO()):
            rc = finish_run([_row()], prefix, "json", False, blocked=False,
                            stop_reason="content_unparsed", pages_requested=3,
                            pages_completed=1, start_url="u", final_url="u")
        eq("a page that would not parse ends the run as partial",
           rc, EXIT_PARTIAL)

        # 4. The catalogue moving underneath the run is recorded, not hidden.
        with redirect_stdout(io.StringIO()):
            finish_run([_row()], prefix, "json", False, blocked=False,
                       stop_reason="completed", pages_requested=2,
                       pages_completed=2, total_results_first=672,
                       total_results_last=671, start_url="u", final_url="u")
        meta = json.load(open(f"{prefix}.meta.json", encoding="utf-8"))
        check("a total_results that moved mid-run is flagged",
              meta["catalog_mutated"] is True)
        with redirect_stdout(io.StringIO()):
            finish_run([_row()], prefix, "json", False, blocked=False,
                       stop_reason="completed", pages_requested=2,
                       pages_completed=2, total_results_first=672,
                       total_results_last=672, start_url="u", final_url="u")
        meta = json.load(open(f"{prefix}.meta.json", encoding="utf-8"))
        check("and a steady one is not", meta["catalog_mutated"] is False)

    # 5. merge_pages records the overlap instead of gating on it: an
    #    insertion ahead of the cursor repeats one listing and loses nothing,
    #    so failing on it would turn every insertion into a false partial.
    merged, stats = output_writer.merge_pages([
        (1, [_row(sku="a"), _row(sku="b")]),
        (2, [_row(sku="b"), _row(sku="c")]),
    ])
    eq("an overlapping page still merges to one row per sku",
       [r.sku for r in merged], ["a", "b", "c"])
    eq("with the rows it started from counted", stats.rows_before_dedupe, 4)
    eq("and the repeat recorded", stats.duplicate_skus_across_pages, 1)
    eq("against the page it came from", stats.duplicate_pages, [2])
    merged, stats = output_writer.merge_pages([
        (2, [_row(sku="c")]), (1, [_row(sku="a")])])
    eq("pages merge in page order however they arrived",
       [r.sku for r in merged], ["a", "c"])


def check_csv_is_not_a_formula():
    """A scraped title is site-controlled text, and a CSV is opened in Excel."""
    print("\n[csv injection]")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "out.csv")
        output_writer.write_csv(
            [_row(sku="1", title='=HYPERLINK("http://evil","Click")'),
             _row(sku="2", title="+1-800-EVIL"),
             _row(sku="3", title="Gravity (India)")], path)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        check("a formula in a title is neutralised",
              rows[0]["title"].startswith("'="))
        check("and so is one that starts with a sign",
              rows[1]["title"].startswith("'+"))
        eq("ordinary text is left exactly as it was",
           rows[2]["title"], "Gravity (India)")

    # The columns that made the naive fix wrong: profit growth is
    # legitimately negative, and a quote in front of it would corrupt it.
    eq("a negative number keeps its sign and its type",
       output_writer.csv_safe(-1234.5), -1234.5)
    eq("and None stays None", output_writer.csv_safe(None), None)


def check_writes_are_atomic():
    """A crash mid-write must not leave a truncated file where a good one was."""
    print("\n[atomic output]")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "out.json")
        output_writer.write_json([_row(sku="good")], path)

        def explode(f):
            f.write('[{"sku": "hal')
            raise RuntimeError("disk full")

        try:
            output_writer._atomic_write(path, explode)
        except RuntimeError:
            pass
        eq("a failed write leaves the previous file intact",
           [r["sku"] for r in json.load(open(path, encoding="utf-8"))], ["good"])
        leftovers = [n for n in os.listdir(tmp) if n.startswith(".tmp-")]
        eq("and no temporary file behind", leftovers, [])




def check_cli_refuses_impossible_values():
    """Bounds on the flags whose out-of-range values change what a run means."""
    print("\n[cli bounds]")
    import argparse as _argparse
    for value in ("0", "-1"):
        try:
            cli_types.positive_int(value)
            check(f"--pages/--retries {value} is refused", False)
        except _argparse.ArgumentTypeError:
            check(f"--pages/--retries {value} is refused", True)
    eq("a sane page count passes through", cli_types.positive_int("3"), 3)
    try:
        cli_types.non_negative_float("-1")
        check("a negative delay is refused", False)
    except _argparse.ArgumentTypeError:
        check("a negative delay is refused", True)
    check("a URL with no scheme is refused",
          bool(cli_types.check_listing_url("www.screener.in/market/IN08/")))
    eq("a real listing URL passes",
       cli_types.check_listing_url("https://www.screener.in/market/IN08/"), [])
    check("the expected host is recognised",
          cli_types.host_is_expected("https://www.screener.in/market/IN08/"))
    check("and a lookalike is not",
          not cli_types.host_is_expected("https://screener.in.evil.test/x"))




def check_diff_refuses_a_window_comparison():
    """`complete` answers a different question from `covers the whole listing`.

    Two three-page runs of a 27-page listing are both complete. A listing that
    merely moved from page 3 to page 4 between them is absent from the second
    file, and reporting that as `removed` reads as "this company left".
    """
    print("\n[diff coverage]")
    import diff_runs

    def _write(prefix, skus, coverage, status="complete", **extra):
        with open(f"{prefix}.json", "w", encoding="utf-8") as f:
            json.dump([{"sku": s, "price": 100} for s in skus], f)
        meta = {"status": status, "coverage": coverage, "pages_completed": 3,
                "pages_requested": 3, "stop_reason": "completed",
                "total_results": 672,
                "start_url": "https://www.screener.in/market/IN08/"}
        meta.update(extra)
        with open(f"{prefix}.meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f)

    with tempfile.TemporaryDirectory() as tmp:
        old = os.path.join(tmp, "old")
        new = os.path.join(tmp, "new")
        out = os.path.join(tmp, "diff.json")
        real_argv = sys.argv

        def run(*flags):
            sys.argv = ["diff_runs.py", "--old", f"{old}.json",
                        "--new", f"{new}.json", "--out", out, *flags]
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = diff_runs.main()
            return rc, buf.getvalue(), json.load(open(out, encoding="utf-8"))

        try:
            # Two window runs: the listing that "vanished" may just be on the
            # next page, so the assortment halves are not conclusions.
            _write(old, ["a", "b"], "window")
            _write(new, ["a", "c"], "window")
            rc, text, result = run()
            eq("a window diff still runs", rc, 0)
            check("but says the assortment is not comparable",
                  result["assortment_comparable"] is False)
            check("and explains why in words the reader can act on",
                  "WINDOW" in text and "left-window" in text)
            rc, _text, _result = run("--fail-on-change")
            eq("--fail-on-change does not fire on a window's added/removed",
               rc, 0)

            # Same two files, both runs exhaustive: now removed means removed.
            _write(old, ["a", "b"], "exhaustive")
            _write(new, ["a", "c"], "exhaustive")
            rc, _text, result = run()
            check("two exhaustive runs ARE comparable",
                  result["assortment_comparable"] is True)
            eq("and the diff says what left the listing",
               [p["sku"] for p in result["removed"]], ["b"])
            rc, _text, _result = run("--fail-on-change")
            eq("--fail-on-change fires on a real assortment change", rc, 1)

            # A price change is comparable either way.
            _write(old, ["a"], "window")
            with open(f"{new}.json", "w", encoding="utf-8") as f:
                json.dump([{"sku": "a", "price": 200}], f)
            _write(new, [], "window")
            with open(f"{new}.json", "w", encoding="utf-8") as f:
                json.dump([{"sku": "a", "price": 200}], f)
            rc, _text, _result = run("--fail-on-change")
            eq("a price change on a sku both runs hold still fires", rc, 1)

            # A catalogue edited mid-run is called out on its own.
            _write(old, ["a"], "exhaustive")
            _write(new, ["a"], "exhaustive", catalog_mutated=True,
                   total_results_first=672, total_results_last=671)
            _rc, text, result = run()
            check("a run that raced the catalogue is flagged in the diff",
                  result["assortment_comparable"] is False
                  and "edited" in text)

            # An older sidecar with no coverage key at all.
            _write(old, ["a"], None)
            _write(new, ["a"], None)
            _rc, text, _result = run()
            check("a sidecar written before coverage existed says so",
                  "unknown" in text)

            # And the refusal that was already there still refuses.
            _write(old, ["a"], "exhaustive", status="partial")
            _write(new, ["a"], "exhaustive")
            sys.argv = ["diff_runs.py", "--old", f"{old}.json",
                        "--new", f"{new}.json"]
            with redirect_stdout(io.StringIO()):
                rc = diff_runs.main()
            eq("a partial run is still refused outright", rc, 2)

            # Two different listings — or one under another sort — are
            # different questions, not two answers to one.
            _write(old, ["a"], "exhaustive")
            _write(new, ["a"], "exhaustive",
                   start_url="https://www.screener.in/market/IN08/?sort=name")
            with redirect_stdout(io.StringIO()):
                rc = diff_runs.main()
            eq("a diff between different listings or sorts is refused", rc, 2)
            _write(new, ["a"], "exhaustive",
                   start_url="https://www.screener.in/market/IN08/?page=3")
            sys.argv = ["diff_runs.py", "--old", f"{old}.json", "--new",
                        f"{new}.json", "--out", out]
            with redirect_stdout(io.StringIO()):
                rc = diff_runs.main()
            eq("while a different START page of the same listing is not", rc, 0)
        finally:
            sys.argv = real_argv




def _outcome_from_fixture(engine, name, page_num, url, category=None):
    """What an engine's _fetch_one_page would return for this real capture.

    Built through the REAL classifier and parser — only the browser is
    stubbed — so the planning logic in scrape() runs against real answers.
    """
    html, fixture_url = fx(name)
    outcome = engine.PageOutcome(page_num=page_num, url=url)
    outcome.state = page_flow.classify(html, url=fixture_url, requested_page=page_num)
    outcome.final_url = fixture_url
    if outcome.state.state == page_flow.CONTENT:
        outcome.products = parse_products(html, fixture_url, category=category,
                                          page=page_num)
        outcome.parse_failed = not outcome.products
    return outcome


def check_engine_flows_on_real_answers():
    print("\n[engine flows]")
    base = "https://www.screener.in/market/IN08/IN0801/IN080101/"
    scenarios = (
        # (label, --url, --pages, {site page: fixture}, exit, status/None, stop)
        ("two pages of a 7-page sector", base, 2,
         {1: "market_p1", 2: "market_p2"}, 0, "complete", "completed"),
        ("a run started on the LAST page, asking for 50", base + "?page=7", 50,
         {7: "market_p7_last"}, 0, "complete", "listing_exhausted"),
        ("a run started past the end", base + "?page=8", 1,
         {8: "market_p8_out_of_range"}, EXIT_FETCH_FAILED, None, None),
        ("the registration wall", "https://www.screener.in/screens/59/magic-formula/", 1,
         {1: "cdp_register_wall"}, EXIT_BLOCKED, None, None),
        ("a screen that does not exist", "https://www.screener.in/screens/999999999/nope/", 1,
         {1: "not_found"}, EXIT_FETCH_FAILED, None, None),
        ("Chromium's own error page", base, 1,
         {1: "chromium_proxy_error"}, EXIT_BLOCKED, None, None),
    )
    import asyncio
    for engine_name, engine in ENGINES.items():
        is_async = inspect.iscoroutinefunction(engine._fetch_one_page)

        class _Session:
            def __init__(self, *a, **kw):
                self.pool = None

            def open(self):
                return self

            def close(self):
                pass

            def relaunch(self):
                pass

        class _AsyncSession(_Session):
            async def open(self):
                return self

            async def close(self):
                pass

            async def relaunch(self):
                pass

        class _PW:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        saved = {k: getattr(engine, k) for k in ("_fetch_one_page", "_BrowserSession")
                 if hasattr(engine, k)}
        if hasattr(engine, "sync_playwright"):
            saved["sync_playwright"] = engine.sync_playwright
        try:
            for label, url, pages, served, want_rc, want_status, want_stop in scenarios:
                fetched = []

                def fake(session, args, pool, page_num, page_url_, _served=served,
                         _fetched=fetched):
                    _fetched.append(page_num)
                    if page_num not in _served:
                        # Recorded, not raised: an unplanned fetch must FAIL
                        # this check, not be swallowed as a failed page.
                        _fetched.append(f"unplanned:{page_num}")
                        name = next(iter(_served.values()))
                    else:
                        name = _served[page_num]
                    return _outcome_from_fixture(engine, name, page_num,
                                                 page_url_, args.category)

                async def afake(*a, **kw):
                    return fake(*a, **kw)

                engine._fetch_one_page = afake if is_async else fake
                engine._BrowserSession = _AsyncSession if is_async else _Session
                if hasattr(engine, "sync_playwright"):
                    engine.sync_playwright = lambda: _PW()
                with tempfile.TemporaryDirectory() as tmp:
                    args = engine.parse_args(["--url", url, "--pages", str(pages),
                                              "--delay", "0", "--format", "json",
                                              "--out", os.path.join(tmp, "run")])
                    with redirect_stdout(io.StringIO()):
                        rc = engine.scrape(args)
                    eq(f"{engine_name}: {label} exits {want_rc}", rc, want_rc)
                    meta_path = os.path.join(tmp, "run.meta.json")
                    if want_status:
                        meta = json.load(open(meta_path, encoding="utf-8"))
                        eq(f"{engine_name}: {label} is {want_status}/{want_stop}",
                           (meta["status"], meta["stop_reason"]), (want_status, want_stop))
                    else:
                        check(f"{engine_name}: {label} writes no sidecar",
                              not os.path.exists(meta_path))
                    eq(f"{engine_name}: {label} fetches exactly the pages it should",
                       fetched, sorted(served))
        finally:
            for k, v in saved.items():
                setattr(engine, k, v)
    if not ENGINES:
        skip("engine flows", "no engine library installed")

    # A driver exception mid-run must not throw away the pages already read.
    for engine_name, engine in ENGINES.items():
        is_async = inspect.iscoroutinefunction(engine._fetch_one_page)
        saved = engine._fetch_one_page

        def boom(session, args, pool, page_num, url):
            raise RuntimeError("Target closed ws://u:" + "leakme@cb.2captcha.com:9222")

        async def aboom(*a, **kw):
            return boom(*a, **kw)
        try:
            engine._fetch_one_page = aboom if is_async else boom
            coro = engine._fetch_safely(None, None, None, 5, "u")
            out = asyncio.run(coro) if is_async else coro
            check(f"{engine_name}: a fetch that raises becomes a failed page, not a crash",
                  out.raised and not out.ok)
        finally:
            engine._fetch_one_page = saved

    # The HTTP engine runs the same scenarios through its own loop.
    import scraper_api_client as sac
    real_fetch = sac.fetch_html
    try:
        for label, url, pages, served, want_rc, _st, _sp in scenarios:
            def fake_fetch(args, page_url_, _served=served):
                n = product_parser.requested_page_number(page_url_)
                return fx(_served[n])[0], None
            sac.fetch_html = fake_fetch
            with tempfile.TemporaryDirectory() as tmp:
                sys_argv = sys.argv
                sys.argv = ["scraper_api_client.py", "--url", url, "--pages", str(pages),
                            "--delay", "0", "--format", "json", "--key", "k" * 32,
                            "--out", os.path.join(tmp, "run")]
                try:
                    args = sac.parse_args()
                finally:
                    sys.argv = sys_argv
                with redirect_stdout(io.StringIO()):
                    rc = sac.scrape(args)
                # The HTTP client reads its fixtures without a browser URL, so
                # a redirect to /register/ is seen from the page itself.
                eq(f"scraper_api_client: {label} exits {want_rc}", rc, want_rc)
    finally:
        sac.fetch_html = real_fetch


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    print("screener-scraper offline suite")
    print("=" * 62)
    for group in (check_parser_values, check_parser_refuses_to_guess,
                  check_page_info_and_pagination, check_page_states,
                  check_markers_on_good_and_cdp_pages,
                  check_shortfall_and_readiness, check_url_refusals,
                  check_fingerprint_kwargs_are_ones_playwright_accepts,
                  check_captcha_solver_units, check_solve_budget_is_enforced,
                  check_credentials_never_leak,
                  check_remote_connect_failures_are_redacted,
                  check_proxy_rotation, check_proxy_preflight,
                  check_output_contract, check_engine_parity,
                  check_readiness_wait_is_csp_safe,
                  check_engine_imports_driver_at_module_level,
                  check_no_undefined_names, check_shared_calls_bind,
                  check_no_code_after_a_terminator,
                  check_banned_wording, check_removed_flags_stay_removed,
                  check_env_example_matches_code, check_env_precedence,
                  check_policy_constants_have_consumers,
                  check_dockerfile_copies_what_it_imports, check_sample_output,
                  check_ci_checks_are_one_implementation,
                  check_oldest_supported_python_can_parse_it,
                  check_packaging_matches_the_tree,
                  check_credentialled_paths_run,
                  check_concurrency_machinery,
                  check_worker_pools_start_on_different_exits,
                  check_run_integrity, check_csv_is_not_a_formula,
                  check_writes_are_atomic,
                  check_cli_refuses_impossible_values,
                  check_diff_refuses_a_window_comparison,
                  check_engine_flows_on_real_answers):
        group()

    print("\n" + "=" * 62)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped")
    for line in SKIPPED:
        print(f"  SKIPPED: {line}")
    if SKIPPED_GROUPS:
        # Printed in a shape CI greps for: "skipped, engine absent" reads
        # identically to a real import error.
        print(f"{len(SKIPPED_GROUPS)} group(s) of checks SKIPPED: "
              + ", ".join(SKIPPED_GROUPS))
    for line in FAILED:
        print(f"  FAILED: {line}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
