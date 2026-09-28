"""
screener-scraper — plain HTTP edition (no browser, no key)
===========================================================

screener.in serves its result tables in the HTML itself: `tr[data-row-
company-id]` rows are in the first response, with no JavaScript to run.
Measured 2026-09-25 with plain curl, and by a third-party audit on
2026-09-27: 0.71 s of process time for page 1 against 7.45 s through
Playwright, about 49 MB of RSS against 144 MB, and no 192 MB browser on
disk. This engine is that GET, fed to the same classifier, parser and
output contract as every other engine here.

It is OPTIONAL, not the default: the README's quick start, the Docker image
and the canary still use Playwright. Whether to switch is a separate
decision (see CHANGELOG).

What it does and does not do
----------------------------
- One `requests.Session`, so pages reuse a connection. Separate connect and
  read timeouts.
- Transient faults are retried, up to `--retries` attempts in total, the
  same count the browser engines use:
    * a connection error or timeout;
    * HTTP 429 or 5xx.
  The pause honours the server's `Retry-After` (capped), and otherwise
  backs off exponentially from `--retry-delay`, with jitter. A 403 or a
  registration wall is NOT retried from the same address: page_flow calls
  that blocked (exit 3), exactly as for the browser engines.
- An honest User-Agent naming this project. Plain curl, with curl's own
  User-Agent, was served every table measured, so there is no browser to
  imitate.
- `--proxy` / `--proxy-file`, rotated per run or per page. There is no
  `--proxy-block-retries` here: a blocked page ends the run, and a different
  exit is a new run.
- Sequential only, with `--delay` between pages. No `--concurrency`: a
  faster way to send N requests from one address is not a feature.
  screener.in's robots.txt disallows `?page=`; every engine warns when a run
  asks for more than page 1 (README, "Legal").

Usage
-----
    python3 http_scraper.py \\
        --url "https://www.screener.in/market/IN08/IN0801/IN080101/" \\
        --pages 1 --out it_software

Requires: pip install -r requirements.txt (or requirements.lock)
"""

import argparse
import logging
import random
import sys
import time
from typing import Optional

import requests

import cli_types
import env_config
import listing_run
from listing_run import FetchFailed
from proxy_pool import (ROTATE_MODES, ProxyError, check_exit_or_raise,
                        from_args as proxy_pool_from_args,
                        install_log_redaction, mask, redact_secret_patterns)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
install_log_redaction()
logger = logging.getLogger("http_scraper")

PROJECT_URL = "https://github.com/2scraper/screener-scraper"
USER_AGENT = f"screener-scraper-http (+{PROJECT_URL})"
CONNECT_TIMEOUT = 10
# A Retry-After this long or longer is not waited out: the server is saying
# "not today", and a run that sleeps for an hour looks exactly like a hang.
MAX_RETRY_AFTER = 120
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _retry_after(resp) -> Optional[float]:
    """Seconds from a Retry-After header, or None. HTTP-date form is ignored."""
    value = (resp.headers.get("Retry-After") or "").strip()
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


def backoff(args, attempt: int, retry_after: Optional[float] = None,
            rng: random.Random = random) -> float:
    """How long to wait before attempt `attempt + 1`.

    The server's own Retry-After wins, capped. Otherwise exponential from
    --retry-delay with ±50% jitter, so two runs started together do not
    retry in lockstep.
    """
    if retry_after is not None:
        return min(retry_after, MAX_RETRY_AFTER)
    return args.retry_delay * (2 ** (attempt - 1)) * rng.uniform(0.5, 1.5)


class HttpTransport:
    """listing_run's `fetch` for a plain GET, with its own session and exit."""

    def __init__(self, args, pool, session: Optional[requests.Session] = None,
                 sleep=time.sleep):
        self.args = args
        self.pool = pool
        self.sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT,
                                     "Accept": "text/html,application/xhtml+xml",
                                     "Accept-Language": "en"})
        self._apply_exit()

    def _apply_exit(self):
        if self.pool:
            # Credentials ride in the proxies mapping, never in argv or a log.
            self.session.proxies = {"http": self.pool.current,
                                    "https": self.pool.current}
            logger.info("Using proxy exit %s", mask(self.pool.current))

    def before_page(self, page_num: int) -> None:
        if self.pool and self.pool.rotates_per_page():
            self.pool.advance(f"per-page rotation before page {page_num}")
            self._apply_exit()

    def __call__(self, args, url: str):
        last_error = None
        for attempt in range(1, args.retries + 1):
            logger.info("GET %s", url)
            try:
                resp = self.session.get(url, timeout=(CONNECT_TIMEOUT, args.timeout))
            except requests.RequestException as e:
                # requests puts the full URL — a proxy's credentials included,
                # for a ProxyError — into the message.
                last_error = redact_secret_patterns(str(e))
                if attempt < args.retries:
                    pause = backoff(args, attempt)
                    logger.warning("GET failed (attempt %d/%d: %s) — retrying in "
                                   "%.1fs.", attempt, args.retries, last_error, pause)
                    self.sleep(pause)
                    continue
                raise FetchFailed(f"GET {url} failed after {attempt} attempt(s): "
                                  f"{last_error}") from None
            if resp.status_code in RETRY_STATUSES and attempt < args.retries:
                pause = backoff(args, attempt, _retry_after(resp))
                logger.warning("HTTP %d from %s (attempt %d/%d) — retrying in %.1fs.",
                               resp.status_code, url, attempt, args.retries, pause)
                self.sleep(pause)
                continue
            logger.info("HTTP %d, %d bytes%s.", resp.status_code, len(resp.content),
                        f", redirected to {resp.url}" if resp.url != url else "")
            # The last answer goes to the classifier whatever its status: a
            # 429 that outlasted the retries is a refusal (exit 3), a 5xx is
            # the site failing (exit 5) — page_flow already tells them apart.
            return resp.text, resp.status_code, resp.url
        raise FetchFailed(f"GET {url}: no attempt made")  # unreachable: retries >= 1


def build_parser():
    p = argparse.ArgumentParser(
        description="screener.in scraper — plain HTTP edition (no browser, no key)")
    p.add_argument("--url", default=None,
                   help="A screener.in screen or sector listing URL. Required "
                        "unless SCREENER_URL is set.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Defaults to the "
                        "listing's own heading, read from the page.")
    p.add_argument("--pages", type=cli_types.positive_int, default=1,
                   help="Number of pages to fetch (25 rows each). Pages after "
                        "the first are ?page= URLs, which robots.txt disallows.")
    p.add_argument("--delay", type=cli_types.non_negative_float, default=2.0,
                   help="Seconds between pages (default 2.0).")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="screener_results", help="Output file prefix")
    p.add_argument("--timeout", type=cli_types.positive_int, default=30,
                   help="Read timeout per request, seconds (default 30).")
    p.add_argument("--retries", type=cli_types.positive_int, default=3,
                   help="Attempts per page for a transient fault — a connection "
                        "error, a timeout, a 429 or a 5xx (default 3, as in the "
                        "browser engines). A refusal is not retried.")
    p.add_argument("--retry-delay", type=cli_types.non_negative_float, default=2.0,
                   help="Base of the exponential backoff, seconds (default 2.0). "
                        "A Retry-After header overrides it.")
    p.add_argument("--proxy", default=None,
                   help="One proxy URL. Better set as SCREENER_PROXY in .env — "
                        "a password on the command line lands in shell history.")
    p.add_argument("--proxy-file", default=None,
                   help="A file of proxy URLs, one per line.")
    p.add_argument("--proxy-rotate", choices=ROTATE_MODES, default="per-run",
                   help="With --proxy-file: one exit for the run, or a new one "
                        "per page.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="With --proxy-file: start from a random exit.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when the site reports 0 rows.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success as "
                        "well as failure.")
    return p


def parse_args(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    # No key and no CDP endpoint: this engine uses neither, and mapping them
    # would make a .env written for the browser engines route this one
    # through a paid product without the user asking.
    env_config.apply(args, keys={"SCREENER_PROXY": "proxy",
                                 "SCREENER_URL": "url"})
    cli_types.finish_args(p, args, logger)
    return args


def scrape(args, session: Optional[requests.Session] = None,
           sleep=time.sleep) -> int:
    pool = proxy_pool_from_args(args)
    check_exit_or_raise(pool, args.url)
    transport = HttpTransport(args, pool, session=session, sleep=sleep)
    return listing_run.run(args, transport, engine="http",
                           refetch_on_policy=False, logger=logger,
                           before_page=transport.before_page)


if __name__ == "__main__":
    try:
        sys.exit(scrape(parse_args()))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list, named as such.
        logger.error("%s", e)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception:  # noqa: BLE001 — a traceback is a log, as in the twins
        import traceback
        logger.error("Crashed:\n%s", redact_secret_patterns(traceback.format_exc()))
        sys.exit(1)
