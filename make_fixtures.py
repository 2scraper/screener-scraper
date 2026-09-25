#!/usr/bin/env python3
"""
make_fixtures.py
-----------------
Builds `fixtures_generated.json` — the offline suite's fixtures — from the
raw captures in `captures/` (which are never committed).

    python3 make_fixtures.py          # rewrite fixtures_generated.json
    python3 make_fixtures.py --check  # verify only; exit 1 on any drift

Every fixture is a REAL capture, cut down and scrubbed, and each cut is
verified before it is written:

  * **Trimmed.** Inline <script> bodies, <style>, <svg> and the footer carry
    no data and are most of a page's bytes; they go. Script TAGS with a
    `src` stay, because the asset-host count and the extension-injection
    check both read them. A listing keeps its first rows, its LAST row and
    the header row the site repeats mid-table, so the repeated header is
    still there to be skipped.
  * **Verified.** Every row kept is compared, column by column, against the
    same row parsed from the untrimmed capture, and the page's own
    classification must be unchanged. A trim that changes what the parser
    sees is refused rather than written.
  * **Scrubbed.** A capture carries the session that fetched it — the
    `csrfmiddlewaretoken` form field and the `csrftoken` cookie value — and a
    /screens/ page names the screen's author ("by <name>", /user/<id>/),
    which is a person. All of those are replaced with obvious placeholders,
    and the script refuses to write a fixture in which any of them survive.
"""

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

import page_flow
import product_parser

REPO = Path(__file__).resolve().parent
CAPTURES = REPO / "captures"
OUT = REPO / "fixtures_generated.json"

# name -> (capture file, the URL it was fetched as, rows to keep or None for all)
SOURCES = {
    "market_p1": ("market_IN08_IN0801_IN080101_.html",
                  "https://www.screener.in/market/IN08/IN0801/IN080101/", 3),
    "market_p2": ("market_IN08_IN0801_IN080101__page_2.html",
                  "https://www.screener.in/market/IN08/IN0801/IN080101/?page=2", 3),
    "market_p7_last": ("market_IN080101_page7.html",
                       "https://www.screener.in/market/IN08/IN0801/IN080101/?page=7", 3),
    "market_p8_out_of_range": ("market_IN080101_page8_out_of_range.html",
                               "https://www.screener.in/market/IN08/IN0801/IN080101/?page=8", 2),
    "magic_formula_p1": ("screens_59_magic-formula_.html",
                         "https://www.screener.in/screens/59/magic-formula/", 3),
    "all_stocks_p220": ("last.html",
                        "https://www.screener.in/screens/71064/all-stocks/?page=220", None),
    "golden_crossover": ("s_336509_golden-crossover.html",
                         "https://www.screener.in/screens/336509/golden-crossover/", None),
    "not_found": ("not_found_screen.html",
                  "https://www.screener.in/screens/999999999/nope/", None),
    "register": ("reg.html", "https://www.screener.in/register/", None),
    "login": ("login.html",
              "https://www.screener.in/login/?next=/screens/59/magic-formula/", None),
    "market_index": ("market_index.html", "https://www.screener.in/market/", 5),
    # Fetched over --cdp-endpoint through the Scraping Browser API by this
    # repo's first version: the registration wall, WITH the auto-solve
    # extension's 16 injected hunter scripts. The fixture this family keeps
    # needing — the marker set must score zero on it.
    "cdp_register_wall": ("cdp_register_wall_2026-08-24.html",
                          "https://www.screener.in/register/", None),
    # Chromium's own network-error page, captured through Selenium with a
    # proxy that refuses connections. Carries the site's hostname in its
    # title and not one reference to the site's assets.
    "chromium_proxy_error": ("chromium_proxy_error.html",
                             "https://www.screener.in/market/IN08/IN0801/IN080101/", None),
}

PLACEHOLDER_TOKEN = "SCRUBBED-CSRF-TOKEN"
PLACEHOLDER_AUTHOR = "SCRUBBED-AUTHOR"

_CSRF_FIELD_RE = re.compile(r'(name="csrfmiddlewaretoken"\s+value=")[^"]+(")')
_CSRF_COOKIE_RE = re.compile(r"(csrftoken=)[A-Za-z0-9]+")
_CSRF_JS_RE = re.compile(r"(csrf[_-]?token['\"]?\s*[:=]\s*['\"])[A-Za-z0-9]+(['\"])", re.I)
_AUTHOR_RE = re.compile(r'(<a href="/user/)\d+(/">)[^<]*(</a>)')
_LEFTOVER_TOKEN_RE = re.compile(r"csrf[^\n]{0,40}[A-Za-z0-9]{32,}", re.I)
_LEFTOVER_USER_RE = re.compile(r"/user/\d+/")


def scrub(html: str) -> str:
    html = _CSRF_FIELD_RE.sub(rf"\g<1>{PLACEHOLDER_TOKEN}\g<2>", html)
    html = _CSRF_COOKIE_RE.sub(rf"\g<1>{PLACEHOLDER_TOKEN}", html)
    html = _CSRF_JS_RE.sub(rf"\g<1>{PLACEHOLDER_TOKEN}\g<2>", html)
    html = _AUTHOR_RE.sub(rf"\g<1>0\g<2>{PLACEHOLDER_AUTHOR}\g<3>", html)
    return html


def scrub_problems(html: str):
    problems = []
    if _LEFTOVER_TOKEN_RE.search(html):
        problems.append("a csrf-token-shaped value survived the scrub")
    for m in _LEFTOVER_USER_RE.finditer(html):
        if m.group(0) != "/user/0/":
            problems.append(f"a user link survived the scrub: {m.group(0)}")
    return problems


_INLINE_SCRIPT_RE = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>.*?</script>", re.S | re.I)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.S | re.I)
_SVG_RE = re.compile(r"<svg\b[^>]*>.*?</svg>", re.S | re.I)
_FOOTER_RE = re.compile(r"<footer\b[^>]*>.*?</footer>", re.S | re.I)
_ROW_RE = re.compile(r"<tr data-row-company-id=\"\d+\">.*?</tr>", re.S)
# Any table row, for a page whose table is not of companies (/market/ lists
# industries): trimmed the same way so the fixture keeps its shape, not bulk.
_ANY_ROW_RE = re.compile(r"<tr\b(?![^>]*data-row-company-id)[^>]*>.*?</tr>", re.S)
_BLANK_RE = re.compile(r"\n\s*\n+")


def trim(html: str, keep_rows) -> str:
    for rx in (_INLINE_SCRIPT_RE, _STYLE_RE, _SVG_RE, _FOOTER_RE):
        html = rx.sub("", html)
    if keep_rows is not None:
        rows = list(_ROW_RE.finditer(html)) or list(_ANY_ROW_RE.finditer(html))
        if len(rows) > keep_rows + 1:
            keep = set(range(keep_rows)) | {len(rows) - 1}
            out, last = [], 0
            for i, m in enumerate(rows):
                out.append(html[last:m.start()])
                if i in keep:
                    out.append(m.group(0))
                last = m.end()
            out.append(html[last:])
            html = "".join(out)
    return _BLANK_RE.sub("\n", html)


def _rows(html, url):
    page = product_parser.requested_page_number(url)
    return [{k: v for k, v in asdict(r).items() if k != "scraped_at"}
            for r in product_parser.parse_products(html, url, page=page)]


def build():
    fixtures, problems = {}, []
    for name, (fname, url, keep) in SOURCES.items():
        path = CAPTURES / fname
        if not path.is_file():
            problems.append(f"{name}: capture {fname} is missing from captures/")
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        cut = scrub(trim(raw, keep))

        before, after = page_flow.classify(raw, url=url), page_flow.classify(cut, url=url)
        if before.state != after.state or before.vendor != after.vendor:
            problems.append(f"{name}: trimming changed the classification "
                            f"({before} -> {after})")
        full = {r["sku"]: r for r in _rows(raw, url)}
        for row in _rows(cut, url):
            original = dict(full.get(row["sku"]) or {})
            # position is the one column a trim legitimately changes.
            original.pop("position", None)
            mine = dict(row)
            mine.pop("position", None)
            if original != mine:
                problems.append(f"{name}: row {row['sku']} parses differently "
                                f"after trimming")
        problems += [f"{name}: {p}" for p in scrub_problems(cut)]
        fixtures[name] = {"url": url, "source": fname,
                          "rows_in_capture": len(full), "html": cut}
    return fixtures, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="verify the committed file matches the captures")
    args = ap.parse_args()
    fixtures, problems = build()
    if problems:
        for p in problems:
            print("FAILED:", p)
        return 1
    text = json.dumps(fixtures, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if args.check:
        same = OUT.is_file() and OUT.read_text(encoding="utf-8") == text
        print("fixtures_generated.json is " + ("current" if same else "STALE"))
        return 0 if same else 1
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.name}: {len(fixtures)} fixture(s), {len(text)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
