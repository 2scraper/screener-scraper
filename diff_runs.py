#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku` —
screener.in's own company id (the row's data-row-company-id).

    python3 diff_runs.py --old it_software.2026-09-01.json \\
                          --new it_software.2026-09-25.json

Typical use is a scheduled re-run of one of the engines, kept under a dated
filename, diffed against the previous one:

    python3 playwright_scraper.py --url "$URL" --pages 50 --out "it_$(date +%F)"
    python3 diff_runs.py --old "$(ls -t it_*.json | grep -v meta | sed -n 2p)" \\
                          --new "it_$(date +%F).json" --out diff.json

Three buckets, each keyed on sku:

  added    — sku present in --new, absent from --old
  removed  — sku present in --old, absent from --new: the company left the
             screen or sector, or — on a window run — simply sits on a page
             this run did not fetch (see _assortment_caveats)
  changed  — sku present in both, with a different value in any of
             TRACKED_FIELDS

There is no `source_changed` bucket here, unlike some siblings: every row is
read from the same results table whichever engine fetched it, so there is no
second provenance a price could have come from (output_writer's docstring).

Two runs of DIFFERENT listings — another screen, another sector, or the same
one under another `?sort=` — are refused outright unless --force: on a window
run the sort decides WHICH companies are in the file, so every added/removed
line would be an artefact of the two runs asking different questions.

A row this project's parser could not recover a sku for (None) cannot be
matched across runs at all, so it is counted and reported separately rather
than silently folded into "added"/"removed", which would be wrong on its face.
"""

import argparse
import json
import re
import sys
from typing import Dict, List, Optional, Tuple

TRACKED_FIELDS = ("price", "currency", "market_cap_cr", "pe",
                  "dividend_yield_pct", "net_profit_qtr_cr", "qtr_profit_var_pct",
                  "sales_qtr_cr", "qtr_sales_var_pct", "roce_pct",
                  "ticker", "consolidated", "extra_metrics")


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(products: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for p in products:
        sku = p.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        # A run's own output can already hold a duplicate sku (two rows in the
        # same category, or a rerun of dedupe_by_sku's job on older output
        # written before it existed) — keep the first and count the rest as
        # unmatchable rather than letting one clobber the other silently.
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = p
    return indexed, unmatchable


def diff_products(old: List[dict], new: List[dict]) -> dict:
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed = []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if field_changes:
            changed.append({"sku": sku, "title": after.get("title"),
                            "changes": field_changes})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed.")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  {p.get('price')} {p.get('currency')}")
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  {p.get('price')} {p.get('currency')}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str) -> Tuple[Optional[str], Optional[dict]]:
    """Read the `<out>.meta.json` sidecar beside a run's JSON output.

    Returns (status, meta), or (None, None) when there is no sidecar. Every
    engine here writes one whenever it writes output, so a missing sidecar
    means either output from an older version, or a run that wrote nothing —
    in which case there is no file to diff either.
    """
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _listing_key(url: Optional[str]) -> Optional[str]:
    """A run's listing, reduced to what makes two runs the same question.

    The page parameter is dropped (a run started on ?page=3 still asks about
    the same listing); everything else — path, sort, order — is kept.
    """
    if not url:
        return None
    from urllib.parse import parse_qsl, urlencode, urlparse
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    query = sorted((k, v) for k, v in parse_qsl(parts.query) if k != "page")
    return f"{host}{parts.path.rstrip('/')}?{urlencode(query)}"


def _check_comparable(args) -> bool:
    """Refuse an assortment diff between runs that are not both complete.

    This is the failure mode the sidecar exists for: a run cut short on page
    3 of 10 is missing every listing on pages 4-10, and diffing it against
    yesterday's full run reports all of them as `removed` — reading as "these
    companies left the screen" when in fact they were simply never fetched.
    Prices of the SKUs both runs DID see are still comparable, which is why
    this is a refusal with a --force escape hatch rather than a hard error.
    """
    problems = []
    listings = []
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if meta is not None:
            listings.append((label, _listing_key(meta.get("start_url"))))
        if status is None:
            continue  # no sidecar: nothing to check, see _run_status
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(listings) == 2 and None not in (listings[0][1], listings[1][1]) \
            and listings[0][1] != listings[1][1]:
        problems.append(
            f"the two runs read different listings ({listings[0][1]} vs "
            f"{listings[1][1]}) — another screen, sector or sort order is a "
            f"different question, not a later answer to the same one")
    if not problems:
        return True

    print("[!] Refusing to diff: at least one run is not a complete view of "
          "the listing, so listings that were never fetched cannot be told "
          "apart from ones that left the listing.")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare anyway "
          "(added/removed will include listings that were simply never "
          "fetched).")
    return False


def _assortment_caveats(args) -> List[str]:
    """Reasons the added/removed halves of this diff cannot be trusted.

    A `complete` run is not the same thing as a complete VIEW of the listing.
    `status` answers "did the run get the pages it went for"; `coverage`
    answers "were those pages the whole listing". Two three-page runs of a
    27-page listing are both complete and both windows, and a listing that
    merely moved from page 3 to page 4 between them is absent from the second
    file for a reason that has nothing to do with leaving the listing.

    Price comparison on the SKUs both files hold stays valid either way, which
    is why this annotates the diff instead of refusing it.
    """
    caveats = []
    for label, path in (("--old", args.old), ("--new", args.new)):
        _status, meta = _run_status(path)
        if meta is None:
            continue
        coverage = meta.get("coverage")
        if coverage == "window":
            caveats.append(
                f"{label} ({path}) covered a WINDOW, not the whole listing: "
                f"{meta.get('pages_completed')} page(s) fetched of a listing "
                f"of {meta.get('total_results')} result(s). Companies past "
                f"that window were never fetched.")
        elif coverage == "tail":
            caveats.append(
                f"{label} ({path}) reached the end of the listing but STARTED "
                f"at page {meta.get('start_page')}: companies on the pages "
                f"before it were never fetched.")
        elif coverage is None:
            caveats.append(
                f"{label} ({path}) was written before runs recorded their "
                f"coverage, so whether it holds the whole listing is unknown.")
        if meta.get("catalog_mutated"):
            caveats.append(
                f"{label} ({path}) ran while the catalogue was being edited "
                f"(total_results moved from {meta.get('total_results_first')} "
                f"to {meta.get('total_results_last')}), so a listing may have "
                f"been stepped over between two pages.")
    return caveats


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two screener-scraper JSON outputs by sku (the company id).")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was partial "
                        "or failed, or when the two runs read different listings "
                        "or sort orders. Companies never fetched by one side "
                        "will appear as added/removed.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    result = diff_products(old, new)
    caveats = _assortment_caveats(args)
    result["assortment_comparable"] = not caveats
    result["assortment_caveats"] = caveats
    _print_summary(result)
    if caveats:
        print("[!] added/removed above are NOT joined-or-left conclusions — "
              "read them as entered-window / left-window:")
        for line in caveats:
            print(f"      {line}")
        print("    Value changes on the companies both files hold are "
              "unaffected. For a real assortment diff, run both sides to the "
              "end of the listing.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    # added/removed only count as a change when both files are known to hold
    # the whole listing. Alerting on a listing that simply moved to the next
    # page is the false alarm this whole sidecar exists to prevent.
    changes = [result["changed"]]
    if result["assortment_comparable"]:
        changes += [result["added"], result["removed"]]
    if args.fail_on_change and any(changes):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
