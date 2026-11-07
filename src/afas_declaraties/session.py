"""Opening an authenticated AFAS InSite session.

Both the scheduled job and the reconnaissance tooling need the same thing: a
browser context that is signed in and has actually settled on the application.
This is that, once.

Note on session lifetime: when the tenant suppresses the "Stay signed in?"
prompt, Entra issues a *non-persistent* session cookie which is discarded when
the browser process exits. A profile on disk therefore does not carry a usable
session between runs, and every run signs in afresh. That is acceptable at a
monthly cadence and is the reason no session-keeper workload exists.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from . import onepassword
from .entra import Credentials, EntraLoginError, on_microsoft_login, sign_in

logger = logging.getLogger(__name__)

#: InSite routes seen mid-handshake that must not count as "arrived".
TRANSIENT_PATHS = ("/signin-oidc", "/signin", "/login", "/authenticationhandler")

_SETTLE_JS = """
([host, transient]) => {
    const h = location.hostname;
    if (h !== host && !h.endsWith('.' + host)) return false;
    const path = location.pathname.toLowerCase();
    return !transient.some(m => path.includes(m));
}
"""


def wait_until_settled(page: Page, host: str, *, timeout_ms: int = 60_000) -> None:
    """Block until the browser is genuinely inside the application.

    Both halves matter. The host check proves we came back from the identity
    provider; the path check proves the application finished exchanging the
    authorisation code. Asserting only one lets a transient callback URL pass
    as authenticated, after which the next navigation is bounced to sign-in.
    """
    try:
        page.wait_for_function(_SETTLE_JS, arg=[host, list(TRANSIENT_PATHS)], timeout=timeout_ms)
    except Exception as exc:
        raise EntraLoginError(f"never settled on {host}; stuck at {page.url}") from exc


def credentials(item: str, vault: str) -> Credentials:
    """The sign-in held in 1Password as ``item``, fetched only when a sign-in needs it."""
    return Credentials(
        username=onepassword.get_field(item, "username", vault),
        password=onepassword.get_field(item, "password", vault),
        totp=lambda: onepassword.get_totp(item, vault),
    )


@contextlib.contextmanager
def open_session(
    host: str,
    *,
    profile: Path,
    headless: bool = True,
    item: str | None = None,
    vault: str | None = None,
    allow_login: bool = True,
) -> Iterator[tuple[object, Page]]:
    """Yield ``(context, page)`` signed in and settled on ``host``."""
    item = item or os.environ.get("OP_ITEM_NAME", "Microsoft")
    vault = vault or os.environ.get("OP_VAULT", "Private")
    profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=headless,
            channel="chromium",
            viewport={"width": 1440, "height": 1000},
            locale="nl-NL",
            timezone_id="Europe/Amsterdam",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=60_000)
            if on_microsoft_login(page):
                if not allow_login:
                    raise EntraLoginError("no live session and sign-in was not permitted")
                logger.info("no live session -- signing in")
                sign_in(page, credentials(item, vault))
            else:
                logger.info("existing session accepted")
            wait_until_settled(page, host)
            logger.info("session live at %s", page.url)
            yield context, page
        finally:
            context.close()
