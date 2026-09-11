# syntax=docker/dockerfile:1
#
# One image, five entrypoints (classify | digest | build | slackd | override).
# The Kubernetes workloads all run this image and differ only in `args`.
#
# Target architecture is linux/arm64 (the OCI cloud tier is arm64 nodes);
# amd64 is built as well so the thing can run on a laptop.

FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Amsterdam \
    # Browsers go somewhere world-readable. The default is $HOME/.cache, and
    # HOME moves to /tmp below so the pod can run with a read-only root fs --
    # which would put the browsers on an emptyDir that is empty at boot.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    # Chromium, the op CLI and Python all want a writable HOME. /tmp is the
    # one writable mount the pod is given.
    HOME=/tmp

# ---------------------------------------------------------------------------
# Base OS: timezone (cron schedules are Europe/Amsterdam and the portal renders
# local dates) plus the tools the 1Password apt repo needs.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        tzdata \
    && ln -snf /usr/share/zoneinfo/Europe/Amsterdam /etc/localtime \
    && echo "Europe/Amsterdam" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# 1Password CLI. Credentials for the corporate login are read from a service
# account at runtime, never baked in.
#
# The apt + debsig keyring dance below is copied verbatim from
# calebsargeant/deskbird-booking/Dockerfile. It is fiddly (1Password ships a
# debsig-verify policy, so the key has to land in *two* keyrings under an
# ID-named directory) and it is known-good on both arches. Do not "tidy" it.
# ---------------------------------------------------------------------------
RUN curl -sS https://downloads.1password.com/linux/keys/1password.asc | \
    gpg --dearmor --output /usr/share/keyrings/1password-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/$(dpkg --print-architecture) stable main" | \
    tee /etc/apt/sources.list.d/1password.list && \
    mkdir -p /etc/debsig/policies/AC2D62742012EA22/ && \
    curl -sS https://downloads.1password.com/linux/debian/debsig/1password.pol | \
    tee /etc/debsig/policies/AC2D62742012EA22/1password.pol && \
    mkdir -p /usr/share/debsig/keyrings/AC2D62742012EA22 && \
    curl -sS https://downloads.1password.com/linux/keys/1password.asc | \
    gpg --dearmor --output /usr/share/debsig/keyrings/AC2D62742012EA22/debsig.gpg && \
    apt-get update && apt-get install -y 1password-cli && \
    rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Python dependencies. Pinned set first so this layer caches independently of
# the application source.
# ---------------------------------------------------------------------------
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Chromium + its shared libraries.
#
# `--no-shell` skips chromium-headless-shell, saving ~100 MiB. That is only
# safe because every launch site passes channel="chromium" (see
# src/afas_declaraties/session.py), which runs full Chromium in new-headless
# mode. A plain `p.chromium.launch(headless=True)` resolves to the shell and
# dies with:
#
#   Executable doesn't exist at /ms-playwright/chromium_headless_shell-*/...
#   Looks like Playwright was just installed or updated.
#
# That message points at the install, not at the caller, so it wastes an hour.
# If a new launch site ever needs plain headless, drop --no-shell here rather
# than debugging the browser.
#
# `--with-deps` shells out to apt for the system libs, so clean up after it.
# ---------------------------------------------------------------------------
RUN playwright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* /tmp/* \
    && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"

# Fail the build, not the 23:30 CronJob, if Chromium cannot start and paint.
# Exercises the exact launch shape the application uses.
#
# Skipped when cross-building. buildx emulates the non-native platform with
# QEMU, which does not implement ptrace, so Chromium dies during sandbox setup
# with "ptrace: Function not implemented" -- a property of the emulator, not of
# the image. The native leg still runs this on every build, and the arm64 image
# was verified by launching Chromium on real arm64 hardware.
ARG TARGETPLATFORM
ARG BUILDPLATFORM
RUN python - "$TARGETPLATFORM" "$BUILDPLATFORM" <<'PY' \
    && rm -rf /tmp/build-selftest
import sys

target, build = (sys.argv[1:3] + ["", ""])[:2]
if target and build and target != build:
    print(f"cross-build {build} -> {target}: skipping the Chromium selftest (QEMU has no ptrace)")
    raise SystemExit(0)

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir="/tmp/build-selftest",
        headless=True,
        channel="chromium",
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    page = ctx.new_page()
    page.set_content("<h1 id='probe'>ok</h1>")
    assert page.inner_text("#probe") == "ok"
    assert page.locator("#probe").bounding_box()["width"] > 0, "Chromium rendered nothing"
    ctx.close()
PY

# ---------------------------------------------------------------------------
# The application. --no-deps because requirements.txt above is the authority on
# versions; pyproject.toml only carries the loose ranges.
# ---------------------------------------------------------------------------
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    # Leave exactly one copy of the code in the image, in site-packages.
    # setuptools' build/ tree and the now-redundant src/ would otherwise sit in
    # /app looking authoritative while the interpreter ignores them.
    && rm -rf /app/build /app/src /app/*.egg-info

# ---------------------------------------------------------------------------
# Non-root. uid 10001, group root so the image also works under an arbitrary
# uid (OpenShift-style) if it ever lands somewhere with that policy.
# ---------------------------------------------------------------------------
RUN useradd -r -u 10001 -g root -d /tmp -s /usr/sbin/nologin app \
    && chown -R 10001:root /app
USER 10001

# No Service and no HTTP surface: slackd dials out over Socket Mode and the
# cron jobs are batch. Liveness is an exec probe on a heartbeat file, defined
# in the chart, not a Docker HEALTHCHECK.
ENTRYPOINT ["python", "-m", "afas_declaraties.cli"]
CMD []
