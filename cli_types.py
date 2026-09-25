"""
cli_types.py
-------------
argparse types that refuse a value the engines cannot honour, and the one
post-parse check of --url, shared by all four CLIs so a bound added here
applies everywhere.

These exist because argparse's `type=int` accepts numbers that quietly turn
the run into something other than what was asked for. The measured example:
`--retries 0` made the attempt loop `range(1, 1)`, so the engine never
navigated at all, classified the blank tab as an interstitial and reported
exit 3 "blocked" — a verdict about the site, reached without sending it a
single request, after burning a proxy rotation.
"""

import argparse
from urllib.parse import urlparse

# The only host these engines know how to read. Not a hard refusal: a
# recorded fixture served locally is legitimate, and the parser will simply
# say what it found. A typo'd host is the case this catches.
EXPECTED_HOST_SUFFIX = "screener.in"


def positive_int(value: str) -> int:
    """An int >= 1. For counts where zero means "do nothing, silently"."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number")
    if number < 1:
        raise argparse.ArgumentTypeError(
            f"must be 1 or more, got {number} — zero or less does not mean "
            f"'no limit' here, it means the loop never runs")
    return number


def non_negative_int(value: str) -> int:
    """An int >= 0. For budgets where zero legitimately means "none"."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number")
    if number < 0:
        raise argparse.ArgumentTypeError(f"cannot be negative, got {number}")
    return number


def non_negative_float(value: str) -> float:
    """A float >= 0, for delays. A negative sleep is not a faster run."""
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number")
    if number < 0:
        raise argparse.ArgumentTypeError(f"cannot be negative, got {number}")
    return number


def check_listing_url(url: str) -> list:
    """Return the problems with `url`, worst first; empty means usable.

    Separate from the argparse types because --url may also arrive from the
    environment or .env, and that path has to be checked too.
    """
    problems = []
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        problems.append(
            f"--url must start with http:// or https://, got {url!r}")
    elif not parsed.netloc:
        problems.append(f"--url has no host: {url!r}")
    return problems


def host_is_expected(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host == EXPECTED_HOST_SUFFIX or host.endswith("." + EXPECTED_HOST_SUFFIX)


def finish_args(parser, args, logger, env_hint: str = "SCREENER_URL") -> None:
    """Validate and complete --url after env_config ran.

    After env_config, because --url may have come from .env and an unusable
    value there fails in exactly the same way. Sets `args.start_page`, the
    site page the URL itself asks for, so a run started on `?page=3` fetches
    3, 4, 5 — and labels its rows that way — rather than 3, 2, 3.
    """
    # Imported here: product_parser imports output_writer, and this module
    # is imported by the engines before either is needed.
    import product_parser

    if not args.url:
        parser.error(f"no --url given, and {env_hint} is not set in the "
                     f"environment or in .env.")
    for problem in check_listing_url(args.url):
        parser.error(problem)
    if not host_is_expected(args.url):
        logger.warning("--url points at %s, not %s: the parser reads "
                       "screener.in's own page shape and will most likely "
                       "find nothing there.", urlparse(args.url).hostname,
                       EXPECTED_HOST_SUFFIX)
    else:
        reason = product_parser.unsupported_reason(args.url)
        if reason:
            parser.error(f"cannot read {args.url}: {reason}")
    bad = product_parser.malformed_page_param(args.url)
    if bad is not None:
        # Refused rather than followed: ?page=abc is served as page 1 and
        # ?page=0 as the LAST page (measured 2026-09-25), so either would
        # label its rows with a page they did not come from.
        parser.error(f"--url carries page={bad!r}; the site serves a "
                     f"different page for that than it names. Drop it, or "
                     f"use a whole number from 1.")
    args.start_page = product_parser.requested_page_number(args.url)
    if args.start_page > 1:
        logger.info("--url starts at page %d of the listing; this run fetches "
                    "pages %d-%d.", args.start_page, args.start_page,
                    args.start_page + args.pages - 1)
    # --category is left as given. When it was not given, every row carries
    # the listing's OWN heading ("Magic Formula", "IT - Software Companies"),
    # read by the parser from the page — one rule for both page kinds, where
    # a URL slug would label a screen and leave a sector's codes unreadable.
