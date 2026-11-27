# The Playwright engine (the one the README recommends) with its own
# Chromium, for a scheduled job or a CI canary. Not required for local
# development — `pip install` directly is simpler there.
#
#   docker build -t screener-scraper .
#   docker run --rm -v "$PWD/out:/out" screener-scraper \
#     --url "https://www.screener.in/market/IN08/IN0801/IN080101/" \
#     --pages 3 --out /out/it_software
#
# Credentials go in through the ENVIRONMENT, never on the command line:
#   docker run --rm --env-file .env -v "$PWD/out:/out" screener-scraper ...
# Nothing here bakes in a credential, and CI asserts that: a .env baked into
# an image is a credential published to everyone who can pull it.
FROM python:3.12-slim

WORKDIR /app

# From the lock, hash-checked: the image carries exactly the versions CI
# tested, and a tampered download fails the build instead of shipping.
COPY requirements-playwright.lock ./
# libatomic1 first: Playwright's bundled Node driver needs it just to run
# `playwright install`, i.e. before that command can install any system
# dependency itself (the slim base image does not ship it; the build
# failed with "libatomic.so.1: cannot open shared object file").
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --require-hashes -r requirements-playwright.lock \
    # Playwright's own apt-get for Chromium's shared libraries — not pip
    # packages, so this has to be its own explicit step.
    && playwright install --with-deps chromium

# The entrypoint's transitive local imports, and nothing else. smoke_test.py's
# own check compares this list against the real import graph: every repo in
# this family once shipped an image that died with ModuleNotFoundError on
# every invocation, --help included, because one module was missing here.
COPY captcha_solver.py cli_types.py env_config.py fingerprint_client.py \
     output_writer.py page_flow.py playwright_scraper.py product_parser.py \
     proxy_pool.py diff_runs.py ./

ENTRYPOINT ["python3", "playwright_scraper.py"]
CMD ["--help"]
