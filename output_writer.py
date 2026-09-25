"""
output_writer.py
-----------------
Shared row model + JSON/CSV writers + the run-status/exit-code mapping used by
every engine in this repo.

Family core. The only per-site parts are the `source` constant and the
site-specific columns at the END of `Product`. The prefix keeps the names and
the order the family's non-commerce repos settled on — source, scraped_at,
url, sku, title, then price and currency because this site genuinely
publishes both — so one consumer reads every repo in the family.

Four family columns are deliberately ABSENT, each for a reason that was
checked rather than assumed (a column that is null on every row of every run
should not exist):

  original_price / discount_pct   A share price has no was-price. 0 of the
                  263 rows across the 2026-09-25 captures carry a struck or
                  reduced figure of any kind, and the table has no column
                  for one.
  brand / rating  Not concepts on a stock table; no capture has either.
  price_source    Every row is read from ONE node — the results table's own
                  cells — whichever engine fetched it: plain HTTP (the
                  Scraper API) and a browser serve the same server-rendered
                  table. A column that is one constant on every row of every
                  run carries no provenance.

The metrics are the page's own, and they vary per screen (a sector page
renders 11 columns, "Magic Formula" 14). The nine an anonymous visitor sees on
every page kind measured get their own typed columns; everything else a
screen adds goes into `extra_metrics`, keyed by the site's own metric name and
unit — so the row SCHEMA is fixed (diff_runs.py and every CSV consumer rely on
that) while no column a screen shows is thrown away.
"""

import csv
import json
import os
import tempfile
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

SOURCE = "screener.in"


@dataclass
class Product:
    # --- family prefix: same names, same order, across the whole family ----
    source: str = SOURCE
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""                      # the company's own screener.in page
    # The site's own internal company id, from the row's
    # data-row-company-id attribute. Chosen over the ticker in the URL
    # because it is the site's identifier for the row itself; 108 of 108
    # measured ids mapped to exactly one URL and back.
    sku: Optional[str] = None
    title: Optional[str] = None        # the company name as the table links it
    price: Optional[float] = None      # "CMP" — current market price
    # From the price column's own written unit ("Rs."), never a default: a
    # page whose price header says anything else leaves this null.
    currency: Optional[str] = None
    # The --category label, or the listing's own heading ("IT - Software
    # Companies", "Magic Formula") when none was given.
    category: Optional[str] = None
    # Which page of the listing this row came from — the SITE's page number,
    # what ?page= asked for — and its 1-based position on that page. The pair
    # is unique across a run; smoke_test.py asserts exactly that.
    page: Optional[int] = None
    position: Optional[int] = None

    # --- site-specific, at the end -----------------------------------------
    # The site's own "S.No.", which is GLOBAL across pages (page 2 starts at
    # 26) — so a gap in a merged run is arithmetic. See product_parser.rank_gaps.
    rank: Optional[int] = None
    # The identifier screener.in puts in the company URL, exactly as spelt
    # there: alphabetic ("TCS") or a six-digit number ("532015").
    ticker: Optional[str] = None
    # Whether the linked page (and so these figures) is the CONSOLIDATED
    # view — the URL ends /consolidated/ — or the standalone one. The two are
    # different numbers for the same company; 71 of 108 measured rows linked
    # the consolidated view.
    consolidated: Optional[bool] = None
    market_cap_cr: Optional[float] = None       # Rs. crore
    pe: Optional[float] = None                  # price to earnings
    dividend_yield_pct: Optional[float] = None
    net_profit_qtr_cr: Optional[float] = None   # latest quarter, Rs. crore
    qtr_profit_var_pct: Optional[float] = None  # YoY quarterly profit growth
    sales_qtr_cr: Optional[float] = None        # latest quarter, Rs. crore
    qtr_sales_var_pct: Optional[float] = None   # YoY quarterly sales growth
    roce_pct: Optional[float] = None            # return on capital employed
    # Every other column the page showed, {"<site's metric name> (<unit>)":
    # value}. Null when the page showed none beyond the nine above.
    extra_metrics: Optional[Dict[str, Optional[float]]] = None


def dedupe_by_sku(products: List[Product], seen: Set[str]) -> List[Product]:
    """Drop products whose sku already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a repeating page then re-parses without duplicating its rows into the
    final output. A product with no sku is always kept: there is nothing to
    key a duplicate check on, and dropping it would be a silent data loss
    rather than a duplicate removal.
    """
    fresh = []
    for p in products:
        if p.sku is None or p.sku not in seen:
            if p.sku is not None:
                seen.add(p.sku)
            fresh.append(p)
    return fresh


def _atomic_write(path: str, write: Callable) -> None:
    """Write `path` through a temporary file in the same directory, then rename.

    Writing straight to the final path means a crash, a full disk or a second
    run against the same --out leaves a HALF-written file behind, and no
    consumer can tell a truncated JSON from a short run. os.replace is atomic
    within a filesystem, so a reader sees either the previous complete file or
    the new complete one and never a mixture of the two.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-",
                               suffix=f"-{os.path.basename(path)}")
    try:
        # newline="" for the csv writer's sake; json does not care.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# A leading one of these makes a spreadsheet treat the cell as a formula
# rather than as text. \t and \r are here because Excel strips them and then
# reads what follows.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    """A value as it goes into a CSV cell: a dict as JSON text, else itself."""
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def csv_safe(value):
    """Neutralise a spreadsheet formula in a scraped string.

    Every text column in a row here is written by whoever listed the business:
    a title of `=HYPERLINK("http://...","Click")` is an active formula the
    moment the CSV is opened in Excel, Sheets or LibreOffice, and the CSV is
    exactly what this project tells people to open there.

    Only `str` values are touched. The numeric columns keep their type and
    their sign — monthly_profit is legitimately negative, and prefixing a
    number would corrupt the column to fix an injection that a number cannot
    carry in the first place.
    """
    if isinstance(value, str) and value[:1] in _FORMULA_LEAD:
        return "'" + value
    return value


@dataclass
class MergeStats:
    """What merging the pages of one run had to drop, and from where."""
    rows_before_dedupe: int = 0
    duplicate_skus_across_pages: int = 0
    duplicate_pages: List[int] = field(default_factory=list)


def merge_pages(pages: Iterable[Tuple[int, List[Product]]]
                ) -> Tuple[List[Product], MergeStats]:
    """Merge per-page rows in PAGE order, dropping SKUs seen on an earlier page.

    Shared by every engine so the merge cannot drift between them, and it
    returns what it dropped rather than only the survivors.

    The counts are the point. Offset pagination over a catalogue that is being
    edited underneath the run behaves in two opposite ways, and only one of
    them is visible here:

      an insertion ahead of the cursor shifts the window down, so one listing
      arrives twice. Nothing is lost. It shows up as a duplicate, and it is
      RECORDED rather than treated as a failure — failing on it would turn
      every insertion into a false partial;

      a deletion ahead of the cursor shifts the window up, so one listing is
      stepped over and never fetched. It produces NO duplicate at all, which
      is why a duplicate count can never be the detector for it. What catches
      that one is the site's own total_results, compared between the run's first
      and last page (see finish_run's `catalog_mutated`).
    """
    merged: List[Product] = []
    seen: Set[str] = set()
    stats = MergeStats()
    for page_num, products in sorted(pages, key=lambda item: item[0]):
        stats.rows_before_dedupe += len(products)
        fresh = dedupe_by_sku(products, seen)
        dropped = len(products) - len(fresh)
        if dropped:
            stats.duplicate_skus_across_pages += dropped
            stats.duplicate_pages.append(page_num)
        merged.extend(fresh)
    return merged, stats


def write_json(products: List[Product], path: str) -> None:
    _atomic_write(path, lambda f: json.dump(
        [asdict(p) for p in products], f, ensure_ascii=False, indent=2))


def write_csv(products: List[Product], path: str) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows.
    if not products:
        _atomic_write(path, lambda f: csv.DictWriter(
            f, fieldnames=list(asdict(Product()).keys())).writeheader())
        return
    fieldnames = list(asdict(products[0]).keys())

    def _write(f):
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in products:
            writer.writerow({k: csv_safe(_cell(v)) for k, v in asdict(p).items()})

    _atomic_write(path, _write)


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See page_flow.classify.
EXIT_BLOCKED = 3

def failure_stop_reason(outcome) -> str:
    """Why this page yielded no data, as a `stop_reason`.

    One mapping for every engine: the ternary this replaces was copied into
    four files, and a reason added in one of them stayed missing from the
    other three.
    """
    if getattr(outcome, "lost", False):
        return "worker_lost_page"
    if getattr(outcome, "parse_failed", False):
        # Two different failures reach here and a reader needs to tell them
        # apart: a page that never painted is an infrastructure problem, a
        # page full of listings this code could not read is a schema problem.
        # Neither is the end of the listing, which is what both used to look
        # like.
        # Imported here rather than at module level: page_flow reaches
        # product_parser, which imports Product from this module, and a
        # top-level import would close that loop.
        import page_flow
        state = getattr(outcome, "state", None)
        painted = state is not None and state.state != page_flow.UNPAINTED
        return "content_unparsed" if painted else "page_never_painted"
    if getattr(outcome, "load_failed", False):
        return "page_load_timeout"
    state = getattr(outcome, "state", None)
    return f"blocked_{(state.vendor if state else None) or 'unknown'}"


# Exit code for a run that gathered SOME listings and then stopped early — a
# page-load timeout, or a challenge, on page 3 of 10. The output file is still
# written (throwing away two good pages would be worse), but it is not a
# complete picture, and a consumer that cannot tell the difference will read
# the pages that were never fetched as listings that were delisted.
EXIT_PARTIAL = 6


# Exit code for a run that never GOT its content: a navigation timeout, a
# dead or unauthenticated proxy, a DNS failure, a remote API error, a page
# that never painted or could not be read. Distinct from EXIT_NO_PRODUCTS
# because those are opposite facts: exit 4 is a statement about the
# CATALOGUE ("we asked, and the answer was nothing"), and handing it to a run
# that never reached the site tells a pipeline the listing is empty when
# nothing was read at all.
#
# 5 rather than EXIT_PARTIAL (6): a 6 means "some rows were gathered and the
# output is incomplete", but a run holding nothing writes no output at all,
# so a consumer reading the file on a 6 would find the PREVIOUS run's good
# data, which `save` deliberately leaves in place. Exit 5 promises no file.
# And 5 is the number the family already reserved for a remote API error,
# so the Scraper API engine's failures and a browser engine's are one code.
#
# Applied only to a run holding NOTHING: a timeout on page 7 of 10 is a
# partial run (exit 6, output written).
EXIT_FETCH_FAILED = 5
EXIT_API_ERROR = EXIT_FETCH_FAILED


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the company, and repeating it across 25
    identical rows would both bloat the output and change the schema every
    consumer already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete — the failure mode it exists to prevent is a partial run's
    un-fetched pages being reported as delisted listings.
    """
    path = f"{out_prefix}.meta.json"
    _atomic_write(path, lambda f: json.dump(meta, f, ensure_ascii=False, indent=2))
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             total_results: Optional[int] = None,
             addressable: Optional[bool] = None,
             coverage: Optional[str] = None,
             pages_missing: Optional[List[int]] = None,
             rows_before_dedupe: Optional[int] = None,
             duplicate_skus_across_pages: Optional[int] = None,
             duplicate_pages: Optional[List[int]] = None,
             total_results_first: Optional[int] = None,
             total_results_last: Optional[int] = None,
             catalog_mutated: Optional[bool] = None,
             pages_available: Optional[int] = None,
             rank_gaps: Optional[List[List[int]]] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the listing genuinely
                 ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `pages_failed` lists the pages that did not yield data, BY NUMBER: a count
    stops being a description once pages can be fetched independently and page
    3 can fail while 4 and 5 succeed.

    `total_results` and `pages_available` are the site's own arithmetic
    ("164 results found: Showing page 1 of 7"), which makes "did we get
    everything?" checkable rather than a guess. No cap was found: page 220
    of the 5,477-company "All Stocks" screen was served in full on
    2026-09-25, so an exhaustive run here is the whole listing, not a slice.

    `rank_gaps` lists the site's global row numbers that are missing from
    the merged rows, as [first, last] ranges within the span the run
    covered — the site numbers its rows, so a gap is proof rather than a
    threshold.

    `addressable` records whether pages 2..N could be addressed by URL at all
    (see product_parser.page_url): False means the run had to chain, and that
    --concurrency was refused for this listing.

    `coverage` is the OTHER half of `status`, and the two answer different
    questions. `status` says whether the run got what it was asked for;
    `coverage` says what it was asked for:

      exhaustive — the run reached the end of the listing. Nothing exists
                   beyond what is in this file.
      window     — the run fetched the --pages it was given and stopped
                   there. Listings beyond that window exist and were never
                   looked at, so a listing absent from this file may simply
                   be on page N+1.
      null       — the run did not finish, so neither applies.

    Conflating the two is what let a three-page run of a 27-page listing be
    diffed as if it were the whole catalogue, reporting every listing that
    moved to page 4 as delisted. diff_runs.py branches on `coverage`.

    `pages_missing` lists pages that were requested, were not refused, and
    never came back at all — a worker that died mid-page used to leave no
    trace anywhere in this file.

    `rows_before_dedupe` / `duplicate_skus_across_pages` / `duplicate_pages`
    record what the merge dropped, and `catalog_mutated` says whether
    the site's own total_results changed between this run's first and last
    page. Together they are how a reader judges whether the catalogue was
    being edited underneath the run — see merge_pages for why the duplicate
    count alone cannot answer that.
    """
    return {
        "source": SOURCE,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "total_results": total_results,
        "pages_available": pages_available,
        "addressable": addressable,
        "coverage": coverage,
        "pages_missing": pages_missing or [],
        "rows_before_dedupe": rows_before_dedupe,
        "duplicate_skus_across_pages": duplicate_skus_across_pages,
        "duplicate_pages": duplicate_pages or [],
        "total_results_first": total_results_first,
        "total_results_last": total_results_last,
        "catalog_mutated": catalog_mutated,
        "rank_gaps": rank_gaps or [],
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def save(products: List[Product], out_prefix: str, fmt: str,
         allow_empty: bool = False) -> int:
    """Write JSON/CSV and return a process exit code.

    On zero products, nothing is written at all unless `allow_empty`. A
    page-load timeout that writes `[]` and exits 0 is read by a consuming
    pipeline as a successful run with no rows — and if the file already
    held a good result, that result is now gone: the failure destroyed the
    last known good data.

    `allow_empty=True` is for the legitimate case: a screen that genuinely
    matches nothing (the site says "0 results found"), where an empty file
    is the answer.
    """
    if not products and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(products, f"{out_prefix}.json")
        print(f"[+] Saved {len(products)} rows -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(products, f"{out_prefix}.csv")
        print(f"[+] Saved {len(products)} rows -> {out_prefix}.csv")
    return 0 if products else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_listings" and "listing_exhausted" are both properties of the DATA —
# a page that added nothing new, and the site stating that it served a
# different page than the one asked for (asked for 8 of 7, served 7). There is
# deliberately no selector-based entry: a missing "Next" link is a property of
# markup, an exhausted listing is a property of the catalogue.
COMPLETE_STOP_REASONS = ("completed", "listing_exhausted", "no_new_listings",
                         "no_results")

# Of those, the ones that mean the run reached the END OF THE LISTING rather
# than the end of the pages it was asked for. The distinction is what
# `coverage` publishes, and what diff_runs.py needs: "complete" answers "did
# the run get what it went for", which is NOT the same question as "does this
# file hold the whole listing".
EXHAUSTIVE_STOP_REASONS = ("listing_exhausted", "no_new_listings", "no_results")


def finish_run(products: List[Product], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               total_results: Optional[int] = None,
               addressable: Optional[bool] = None,
               pages_missing: Optional[List[int]] = None,
               merge_stats: Optional[MergeStats] = None,
               total_results_first: Optional[int] = None,
               total_results_last: Optional[int] = None,
               pages_available: Optional[int] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all engines so the status/exit-code mapping cannot drift between
    them.

    The metadata sidecar is written ONLY when the output file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other.

    A run is complete only if its stop reason says so AND every requested page
    is accounted for. The second half is not redundant: `stop_reason` only
    ever knows about pages the engine noticed losing, and the whole shape of
    the bug this guards against is a page disappearing with nobody noticing.
    The page count is checked here, once, for every engine — an engine that
    forgets to report a gap still cannot publish a complete-looking run.
    """
    missing = sorted(pages_missing or [])
    # "completed" means the page loop ran to the end of --pages without a
    # reason to stop early, so fewer completed pages than requested is a
    # contradiction: some page went missing without being counted as failed.
    unaccounted = stop_reason == "completed" and pages_completed < pages_requested
    if missing or unaccounted:
        print(f"[!] {pages_completed} of {pages_requested} requested page(s) came "
              f"back and nothing in the run explains the rest"
              + (f" — pages {', '.join(str(n) for n in missing)} were never "
                 f"reported at all" if missing else "")
              + ". Reporting a PARTIAL run.")
        if stop_reason in COMPLETE_STOP_REASONS:
            stop_reason = "pages_missing"

    complete = (stop_reason in COMPLETE_STOP_REASONS
                and not missing and not unaccounted)
    coverage = ("exhaustive" if stop_reason in EXHAUSTIVE_STOP_REASONS else
                "window" if stop_reason == "completed" else None)
    catalog_mutated = (None if total_results_first is None or total_results_last is None
                       else total_results_first != total_results_last)
    if catalog_mutated:
        print(f"[!] The site's own match count moved from {total_results_first} to "
              f"{total_results_last} while this run was collecting: the catalogue "
              f"was edited underneath it, so a listing may have been stepped over "
              f"between two pages. Recorded as catalog_mutated in the metadata.")
    # Imported here for the same reason as in failure_stop_reason: the parser
    # imports Product from this module.
    from product_parser import rank_gaps as _rank_gaps
    gaps = [list(g) for g in _rank_gaps(products)]
    if gaps:
        print(f"[!] The site numbers its rows, and {len(gaps)} range(s) of rank "
              f"are missing from the merged output: "
              + ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in gaps)
              + ". Rows the site numbered never arrived; recorded as "
                "rank_gaps in the metadata.")
    rc = save(products, out_prefix, fmt, allow_empty=allow_empty)
    wrote_output = bool(products) or allow_empty

    if wrote_output:
        status = "complete" if (products and complete) else (
            "partial" if products else "failed")
        if not products and allow_empty and complete:
            # An explicitly empty result that the site itself reported as
            # empty is a complete answer, not a failure.
            status = "complete"
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, total_results=total_results,
            addressable=addressable, coverage=coverage, pages_missing=missing,
            rows_before_dedupe=(merge_stats.rows_before_dedupe
                                if merge_stats else None),
            duplicate_skus_across_pages=(merge_stats.duplicate_skus_across_pages
                                         if merge_stats else None),
            duplicate_pages=(merge_stats.duplicate_pages if merge_stats else None),
            total_results_first=total_results_first,
            total_results_last=total_results_last,
            catalog_mutated=catalog_mutated, pages_available=pages_available,
            rank_gaps=gaps,
            start_url=start_url, final_url=final_url, products=len(products)))

    if not products:
        # Nothing gathered at all, and WHY decides the code — three different
        # facts a pipeline branches on:
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages: a dead proxy, a load
        #                      timeout, an API error, a page nobody could read
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one would fall silently through to "the catalogue is
        # empty" — the exact defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the listing from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
