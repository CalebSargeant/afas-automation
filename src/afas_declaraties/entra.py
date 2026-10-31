"""Microsoft Entra ID sign-in, driven with Playwright.

Written against the interactive flow only: email -> password -> TOTP -> "Stay
signed in". It deliberately does no credential entry when the tenant already
recognises the persisted session; the caller decides whether a full sign-in is
even needed by checking :func:`is_signed_in` first.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.sync_api import Page

logger = logging.getLogger(__name__)

MICROSOFT_HOSTS = (
    "login.microsoftonline.com",
    "login.microsoft.com",
    "login.live.com",
)

# Entra's markup is stable across tenants; these have not moved in years.
SEL_EMAIL = "input[name='loginfmt']"
SEL_EMAIL_NEXT = "input[type='submit'][value='Next'], #idSIButton9"
SEL_PASSWORD = "input[name='passwd']"
SEL_PASSWORD_SUBMIT = "input[type='submit'][value='Sign in'], #idSIButton9"
SEL_TOTP = "input[name='otc']"
SEL_TOTP_VERIFY = "input[type='submit'][value='Verify'], #idSubmit_SAOTCC_Continue, #idSIButton9"
# The stay-signed-in step must be identified by something unique to it.
# #idSIButton9 is Entra's primary submit on nearly every page, so keying off it
# fires this branch on the password page and submits an empty field.
SEL_KMSI_MARKER = (
    "#KmsiCheckboxField, input[type='submit'][value='Ja'], input[type='submit'][value='Yes']"
)
SEL_KMSI_SUBMIT = (
    "input[type='submit'][value='Ja'], input[type='submit'][value='Yes'], #idSIButton9"
)

# Real failures only. Field-level hints such as "Voer uw wachtwoord in" render
# in [role='alert'] before anything has gone wrong, so that is not an error
# container.
SEL_ERROR = "#passwordError, #usernameError, .alert-error"

# "Een account kiezen" / "Pick an account". Appears whenever the profile still
# holds a partial session, so it cannot be treated as an unexpected state.
SEL_TILES = "#tilesHolder"
SEL_OTHER_TILE = "#otherTile"


class EntraLoginError(RuntimeError):
    """Sign-in did not complete."""


@dataclass(frozen=True)
class Credentials:
    """Credential accessors, not values.

    ``totp`` is a callable so the code is generated at the moment it is typed.
    Fetching it up front burns most of the 30-second window on page loads.
    """

    username: str
    password: str
    totp: Callable[[], str]


def host_matches(url: str, domains: tuple[str, ...]) -> bool:
    """True if the URL's host is, or is a subdomain of, one of ``domains``.

    Deliberately not a substring test. A ``redirectUrl=`` query parameter
    routinely contains the other side's domain, so ``"microsoftonline.com" in
    url`` returns true while sitting on the application's own page.
    """
    hostname = (urlparse(url).hostname or "").lower()
    return any(hostname == d or hostname.endswith(f".{d}") for d in domains)


def on_microsoft_login(page: Page) -> bool:
    return host_matches(page.url, MICROSOFT_HOSTS)


def make_settled_predicate(
    app_host: str,
    transient_paths: tuple[str, ...],
) -> Callable[[Page], bool]:
    """Build the "we have actually arrived" check.

    Both halves are load-bearing. The positive half proves we came back from
    the identity provider; the negative half proves the application has
    finished exchanging the authorisation code and is no longer sitting on a
    callback or landing route. Checking only the negative half is a known
    production failure: transient post-SSO URLs pass as authenticated, the
    script navigates away mid-exchange, and the app bounces it back to the
    sign-in page.
    """

    def settled(page: Page) -> bool:
        if not host_matches(page.url, (app_host,)):
            return False
        path = (urlparse(page.url).path or "").lower()
        return not any(marker in path for marker in transient_paths)

    return settled


#: Entra is a single-page app that keeps every pane in the DOM at once. On the
#: password pane the email input is still ``visibility: visible`` with a
#: non-zero box -- it is merely parked in a corner, aria-hidden and untabbable.
#: Playwright's is_visible() therefore reports BOTH fields as visible on BOTH
#: panes, so it cannot say which step is actually on screen. These three
#: properties can, and none of them depend on the interface language.
_ACTIVE_JS = """
([sel, requireFocusable]) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  if (!el.offsetParent) return false;
  if (el.closest('[aria-hidden="true"]')) return false;
  // Only meaningful for inputs. A container such as the account-picker div is
  // legitimately tabIndex -1, so applying this to everything makes the picker
  // undetectable and the state machine stalls on "waiting".
  if (requireFocusable && el.tabIndex < 0) return false;
  const box = el.getBoundingClientRect();
  return box.width > 0 && box.height > 0;
}
"""


def _is_active(page: Page, selector: str, *, focusable: bool = True) -> bool:
    """True if ``selector`` is the element the user would actually be acting on."""
    try:
        return bool(page.evaluate(_ACTIVE_JS, [selector, focusable]))
    except Exception:  # noqa: BLE001 - navigation mid-evaluate
        return False


def _await_inactive(page: Page, selector: str, *, timeout_ms: int = 20_000) -> None:
    """Wait until ``selector`` stops being the active control.

    Waiting for it to become *hidden* never succeeds -- the pane stays in the
    DOM and stays "visible" -- so a hidden-state wait times out, the loop
    re-detects the same step and clicks the shared primary button a second
    time. By then that button belongs to the next pane, which is how an empty
    password gets submitted.
    """
    try:
        page.wait_for_function(
            f"(a) => {{ const f = {_ACTIVE_JS}; return !f(a); }}",
            arg=[selector, True],
            timeout=timeout_ms,
        )
    except Exception:  # noqa: BLE001 - the loop re-checks anyway
        logger.debug("entra: %s was still active after the transition wait", selector)


def _raise_on_page_error(page: Page) -> None:
    """Surface Entra's own error text instead of dying on a generic timeout.

    Only *visible* containers count. Entra ships these elements pre-populated
    but hidden as part of each pane's markup, so testing for text alone
    reports a failure on a perfectly healthy sign-in.
    """
    for handle in page.query_selector_all(SEL_ERROR):
        try:
            if not handle.is_visible():
                continue
            text = (handle.inner_text() or "").strip()
        except Exception:  # noqa: BLE001 - element detached mid-check
            continue
        if text:
            raise EntraLoginError(f"Entra rejected the sign-in: {text}")


def _fill_verified(page: Page, selector: str, value: str, *, label: str, attempts: int = 4) -> bool:
    """Fill a field and confirm the value stuck before submitting it.

    Entra re-renders panes mid-transition and can silently discard a value
    typed a moment earlier. Reading it back turns a confusing "wrong
    credentials" error into an ordinary retry.
    """
    for attempt in range(attempts):
        try:
            page.fill(selector, value)
            if page.input_value(selector) == value:
                return True
        except Exception:  # noqa: BLE001 - element re-rendered under us
            return False
        logger.debug("entra: %s did not retain its value (attempt %d)", label, attempt + 1)
    return False


def _choose_account_tile(page: Page, username: str) -> bool:
    """Pick the tile for ``username``, else fall back to "use another account"."""
    if not _is_active(page, SEL_TILES, focusable=False):
        return False
    wanted = username.strip().lower()
    for tile in page.query_selector_all(
        f"{SEL_TILES} div[data-test-id], {SEL_TILES} [role='button']"
    ):
        try:
            if tile.is_visible() and wanted and wanted in (tile.inner_text() or "").strip().lower():
                tile.click()
                logger.info("entra: selected the existing account tile")
                return True
        except Exception:  # noqa: BLE001
            continue
    try:
        other = page.query_selector(SEL_OTHER_TILE)
        if other and other.is_visible():
            other.click()
            logger.info("entra: chose 'use another account'")
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def sign_in(page: Page, creds: Credentials, *, timeout_ms: int = 120_000) -> None:
    """Complete an interactive Entra sign-in on ``page``.

    Driven as a state machine rather than a fixed sequence: Entra varies the
    order and omits steps depending on what the profile already holds, so an
    account picker, a skipped password or an absent TOTP prompt are ordinary
    states rather than special cases. Each step waits for its own control to go
    inactive before the next pass, which is what stops the shared primary
    button being clicked twice.
    """
    logger.info("entra: sign-in starting")
    deadline = time.monotonic() + timeout_ms / 1000
    typed_password = False
    last_state = None

    while time.monotonic() < deadline:
        if not on_microsoft_login(page):
            logger.info("entra: left the identity provider")
            return

        _raise_on_page_error(page)

        if _choose_account_tile(page, creds.username):
            state = "tiles"
            _await_inactive(page, SEL_TILES)
        elif _is_active(page, SEL_EMAIL):
            state = "email"
            if _fill_verified(page, SEL_EMAIL, creds.username, label="username"):
                page.click(SEL_EMAIL_NEXT)
                _await_inactive(page, SEL_EMAIL)
        elif _is_active(page, SEL_PASSWORD):
            state = "password"
            if _fill_verified(page, SEL_PASSWORD, creds.password, label="password"):
                page.click(SEL_PASSWORD_SUBMIT)
                typed_password = True
                _await_inactive(page, SEL_PASSWORD)
                logger.info("entra: password submitted")
        elif _is_active(page, SEL_TOTP):
            state = "totp"
            # Fetched here, not earlier: the code lives 30 seconds and the
            # preceding page loads would spend most of that window.
            if _fill_verified(page, SEL_TOTP, creds.totp(), label="TOTP"):
                page.click(SEL_TOTP_VERIFY)
                typed_password = typed_password
                _await_inactive(page, SEL_TOTP)
                logger.info("entra: TOTP submitted")
        elif _kmsi_button(page) is not None:
            state = "kmsi"
            page.click(SEL_KMSI_SUBMIT)
            logger.info("entra: answered the stay-signed-in prompt")
            page.wait_for_timeout(1500)
        else:
            state = "waiting"
            page.wait_for_timeout(1000)

        logger.debug("entra: state=%s url=%s", state, page.url[:90])
        last_state = state

    raise EntraLoginError(
        f"sign-in did not complete within the deadline; last state {last_state!r}, "
        f"password {'was' if typed_password else 'was not'} submitted, "
        f"pane {_pane_title(page)!r}, at {page.url[:120]}"
    )


def _pane_title(page: Page) -> str:
    """The heading of whatever pane stalled the sign-in. A state of "waiting" names nothing."""
    try:
        for handle in page.query_selector_all("[role='heading'], .text-title, #loginHeader"):
            if handle.is_visible() and (text := (handle.inner_text() or "").strip()):
                return text[:120]
    except Exception:  # noqa: BLE001 - this only decorates an error already being raised
        pass
    return ""


def _kmsi_button(page: Page):
    """The stay-signed-in confirm, identified by something unique to that pane."""
    for handle in page.query_selector_all(
        "input[type='submit'][value='Ja'], input[type='submit'][value='Yes']"
    ):
        try:
            if handle.is_visible():
                return handle
        except Exception:  # noqa: BLE001
            continue
    return None
