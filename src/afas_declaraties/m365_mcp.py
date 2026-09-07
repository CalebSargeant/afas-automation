"""Reading Microsoft 365 through Claude's M365 MCP connector.

Why this exists at all: this tenant does not let a user register an
application, so the ordinary Microsoft Graph route is closed. The connector at
``https://microsoft365.mcp.claude.com/mcp`` is a plain streamable-HTTP MCP
server that validates an Entra token issued for an app pair Anthropic
registered multi-tenant and the tenant already consented. A device-code sign-in
against that pair therefore needs no admin action and no new registration, and
``offline_access`` makes it headless after the first browser sign-in.

Nothing here escalates anything. Every scope is delegated, so the reach is
exactly what the signed-in user can already open in Outlook. What changes is
that it becomes scriptable, and that reading a calendar stops needing a browser.

The two GUIDs below are **Anthropic's** public multi-tenant app registrations,
not this tenant's. They are not secrets and they identify no employer, so they
are safe in a public repository. The tenant itself is deliberately absent:
``organizations`` lets Entra resolve the signed-in user's home directory, and
``M365_TENANT`` overrides it if the wrong directory is ever picked.

The cached token, on the other hand, *is* a credential: a refresh token is
standing read access to the mailbox that does not re-prompt for MFA. It is
gitignored and belongs in the secret store, never in the image.

    python -m afas_declaraties.m365_mcp login     # once, interactive
    python -m afas_declaraties.m365_mcp tools
    python -m afas_declaraties.m365_mcp call get_me
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Anthropic's public client ("M365 MCP Client for Claude"). A public client, so
#: the device-code grant works and there is no client secret to hold anywhere.
CLIENT_ID = os.environ.get("M365_CLIENT_ID", "08ad6f98-a4f8-4635-bb8d-f1a3044760f0")
#: The connector's own API ("M365 MCP Server for Claude"), which exposes exactly
#: one scope. ``offline_access`` is what makes every later run headless.
SCOPE = os.environ.get(
    "M365_SCOPE",
    "api://07c030f6-5743-41b7-ba00-0a6e85f37c17/access_as_user offline_access",
)
MCP_URL = os.environ.get("M365_MCP_URL", "https://microsoft365.mcp.claude.com/mcp")
#: ``organizations`` resolves the signed-in user's home tenant, which keeps the
#: tenant GUID out of this public repository. Override it only if the wrong
#: directory is picked: an account that is a guest elsewhere can land in one,
#: and querying the wrong directory gives confidently wrong answers.
TENANT = os.environ.get("M365_TENANT", "organizations")
MCP_PROTOCOL_VERSION = "2025-06-18"

#: A tenant is one path segment: a GUID, a verified domain, or one of Entra's
#: aliases. Interpolating anything else into the authority URL would let an
#: environment variable steer the token request somewhere else entirely.
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Where the refresh token lives. In the cluster this is a writable path under
#: /tmp, seeded once per run from the secret; see :func:`_cache_path`.
CACHE = Path(os.path.expanduser(os.environ.get("M365_TOKEN_CACHE", "~/.m365-mcp-token.json")))
#: A read-only file holding the same JSON, e.g. a mounted Secret.
SEED = os.environ.get("M365_TOKEN_SEED", "")
#: The same JSON inline, which is how it arrives in the cluster: every other
#: credential here rides the release Secret through envFrom, and one env var
#: beats a bespoke volume mount plus a projection of a single key.
SEED_JSON = os.environ.get("M365_TOKEN_JSON", "")

if not _TENANT_RE.match(TENANT):
    raise ValueError(f"M365_TENANT is not a single URL path segment: {TENANT!r}")

_AUTH = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"

#: The connector caps a page at 25 whatever is asked for, so asking for more is
#: not an optimisation. It is a way to believe a short page means the end.
PAGE_LIMIT = 25
#: A page cap, so a pagination bug cannot turn one classification run into an
#: unbounded walk of the mailbox.
MAX_PAGES = 40


class M365Error(RuntimeError):
    """The connector could not be reached, or answered with an error."""


class M365AuthError(M365Error):
    """No usable token, and this process may not stop to ask for one."""


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def _urlopen(url: str, *, data: bytes, headers: dict | None = None, timeout: int):
    """Open an **https** URL, and nothing else.

    Both URLs this module opens are assembled from environment variables --
    ``M365_MCP_URL`` and ``M365_TENANT`` -- and urllib also speaks ``file://``
    and ``ftp://``. Without this check a mistyped or hostile variable turns a
    token request into a local file read whose contents are then posted onward.
    """
    if not url.startswith("https://"):
        raise M365Error(f"refusing to open a non-https URL: {url[:60]!r}")
    request = urllib.request.Request(url, data=data, headers=headers or {})
    # nosec B310 - the scheme is checked immediately above
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310  # nosemgrep


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    try:
        with _urlopen(url, data=body, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # Entra reports OAuth failures as 4xx with the detail in the body, so
        # the body is the interesting half and must not be discarded.
        try:
            return json.loads(exc.read().decode())
        except (ValueError, OSError) as inner:
            raise M365Error(f"{url} -> HTTP {exc.code}") from inner
    except urllib.error.URLError as exc:
        raise M365Error(f"cannot reach {url}: {exc.reason}") from exc


def _cache_path() -> Path:
    """The writable cache, seeded once from a read-only secret if one is given.

    A Kubernetes Secret mounts read-only and Entra rotates the refresh token on
    every use, so the rotated token has to be written somewhere else. Without
    this the job would silently keep replaying the original refresh token until
    it ages out, and then start failing for no visible reason.

    The rotation is lost when the pod exits, which is fine: the seed's own
    refresh token stays valid for its sliding window, and the seed is what the
    next run starts from. Only a run every 90 days is required to keep it alive.
    """
    if CACHE.exists():
        return CACHE
    if SEED and Path(SEED).exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SEED, CACHE)
        CACHE.chmod(0o600)
        # The path, never the contents. Nothing in this module logs a
        # token, an authorisation header or a device code.
        logger.info("m365: cache seeded from %s", SEED)
    elif SEED_JSON.strip():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(SEED_JSON)
        CACHE.chmod(0o600)
        logger.info("m365: cache seeded from the environment")
    return CACHE


def _save(payload: dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
    except OSError as exc:
        # Not fatal for this run, since the access token in hand still works,
        # but the rotated refresh token is now lost. Say so loudly.
        logger.warning(
            "m365: cannot write the token cache at %s (%s); the rotated refresh token "
            "is lost and this will start failing once the current one expires",
            path,
            exc,
        )


def _expiry(access_token: str) -> float:
    """Read ``exp`` out of the JWT without validating it -- Entra already did."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return 0.0


def _device_code() -> dict:
    started = _post_form(f"{_AUTH}/devicecode", {"client_id": CLIENT_ID, "scope": SCOPE})
    if "user_code" not in started:
        raise M365AuthError(
            f"device code request failed: {started.get('error')}: "
            f"{started.get('error_description', '')[:300]}"
        )
    print(
        f"\nOpen {started['verification_uri']} and enter code: {started['user_code']}\n",
        file=sys.stderr,
        flush=True,
    )
    deadline = time.time() + int(started["expires_in"])
    interval = int(started.get("interval", 5))
    while time.time() < deadline:
        answer = _post_form(
            f"{_AUTH}/token",
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": started["device_code"],
            },
        )
        if "access_token" in answer:
            return answer
        if answer.get("error") == "slow_down":
            interval += 5
        elif answer.get("error") != "authorization_pending":
            raise M365AuthError(
                f"sign-in failed: {answer.get('error')}: "
                f"{answer.get('error_description', '')[:300]}"
            )
        time.sleep(interval)
    raise M365AuthError("device code expired before the sign-in completed")


def token(*, allow_device_code: bool = False) -> str:
    """A live access token, refreshed silently from the cache.

    ``allow_device_code`` defaults to False on purpose. A device-code flow
    blocks until a human types a code into a browser, and a scheduled job has no
    human. Failing immediately with a clear message beats a CronJob that hangs
    until its deadline and reports nothing useful.
    """
    path = _cache_path()
    cached: dict | None = None
    if path.exists():
        try:
            cached = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("m365: the token cache at %s is unreadable (%s)", path, exc)

    if cached and "access_token" in cached:
        # 120s of slack, because a token that expires mid-request surfaces as a
        # confusing 401 rather than as an expiry.
        if _expiry(cached["access_token"]) - 120 > time.time():
            return cached["access_token"]

    if cached and cached.get("refresh_token"):
        refreshed = _post_form(
            f"{_AUTH}/token",
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": cached["refresh_token"],
                "scope": SCOPE,
            },
        )
        if "access_token" in refreshed:
            _save(refreshed)
            return refreshed["access_token"]
        logger.warning(
            "m365: the refresh was rejected (%s: %s)",
            refreshed.get("error"),
            refreshed.get("error_description", "")[:200],
        )

    if not allow_device_code:
        raise M365AuthError(
            f"no usable M365 token at {path}. Run "
            "'python -m afas_declaraties.m365_mcp login' and put the resulting file in "
            "the secret store; a scheduled run will not stop to sign in."
        )
    issued = _device_code()
    _save(issued)
    return issued["access_token"]


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

_initialised = False


#: JSON-RPC ids have to be unique within a session. The connector is stateless
#: and answers one request per POST, so reusing 1 happens to work -- but a
#: server that starts rejecting duplicates would fail on the second call with an
#: error naming neither the id nor this module.
_request_id = itertools.count(1)


def _rpc(method: str, params: dict) -> dict:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": next(_request_id), "method": method, "params": params}
    ).encode()
    try:
        response = _urlopen(
            MCP_URL,
            data=body,
            headers={
                "Authorization": "Bearer " + token(),
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            },
            timeout=120,
        )
        with response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        raise M365Error(f"MCP HTTP {exc.code}: {exc.read().decode()[:300]}") from exc
    except urllib.error.URLError as exc:
        raise M365Error(f"cannot reach the MCP connector: {exc.reason}") from exc

    # The server is free to answer as SSE even when JSON was acceptable.
    for line in raw.splitlines():
        if line.startswith("data: "):
            raw = line[6:]
            break
    try:
        answer = json.loads(raw)
    except ValueError as exc:
        raise M365Error(f"MCP returned a non-JSON body: {raw[:200]!r}") from exc
    if "error" in answer:
        error = answer["error"]
        raise M365Error(f"MCP error {error.get('code')}: {error.get('message')}")
    return answer["result"]


def _handshake() -> None:
    global _initialised
    if _initialised:
        return
    _rpc(
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "afas-declaraties", "version": "1"},
        },
    )
    _initialised = True


@dataclass
class Page:
    """One page of a connector search, taken apart into its block kinds.

    The connector answers with a list of content blocks rather than one
    document: an optional ``searchInfo`` header, one block per result, an
    optional pagination footer, and -- when a scan was cut short -- a plain
    prose note. Joining them into a single string, which is the obvious thing to
    do, yields ``}{``-concatenated JSON and loses the note entirely.
    """

    items: list[dict] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    next_offset: int | None = None
    total: int | None = None


#: Keys that mark the trailing pagination block. ``totalResultCount`` can arrive
#: on its own when a search fits in a single page.
_FOOTER_KEYS = {"nextOffset", "moreResults", "totalResultCount", "nextCursor"}


def call(tool: str, **arguments) -> Page:
    """Call one connector tool and take its content blocks apart.

    An unrecognised block is recorded as a note, which marks the whole search
    incomplete, which the calendar reader turns into ``degraded``. Neither
    extreme is right here: skipping it silently would make a moved payload look
    exactly like a quiet week in the calendar, and raising would let one new
    metadata block Anthropic adds -- at an endpoint they own and do not document
    -- break every classification run outright.
    """
    _handshake()
    result = _rpc("tools/call", {"name": tool, "arguments": arguments})
    blocks = result.get("content", [])
    if result.get("isError"):
        raise M365Error(f"{tool}: {' '.join(b.get('text', '') for b in blocks)[:400]}")

    page = Page()
    for block in blocks:
        text = block.get("text", "")
        if not text.strip():
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            # The chat search prepends a prose note when a scan hit a rate limit
            # or its time budget. That is a partial answer, not an empty one.
            page.notes.append(text.strip())
            continue
        if not isinstance(parsed, dict):
            raise M365Error(f"{tool}: unexpected content block {text[:120]!r}")
        if "uri" in parsed or "id" in parsed:
            page.items.append(parsed)
        elif "searchInfo" in parsed:
            page.info = parsed["searchInfo"]
        elif _FOOTER_KEYS & parsed.keys():
            page.next_offset = parsed.get("nextOffset")
            page.total = parsed.get("totalResultCount")
        else:
            logger.error("m365: %s returned an unrecognised block %s", tool, sorted(parsed))
            page.notes.append(f"unrecognised content block: {sorted(parsed)}")
    return page


@dataclass(frozen=True)
class SearchResult:
    """Every page of a search, flattened, with what went wrong still attached."""

    items: list[dict]
    notes: list[str]
    total: int | None
    truncated: bool  # more pages existed than max_pages allowed

    @property
    def complete(self) -> bool:
        """False when the answer is knowingly partial, for any reason."""
        return not self.truncated and not self.notes


def search(tool: str, *, max_pages: int = MAX_PAGES, **arguments) -> SearchResult:
    """Page a connector search to the end and return everything it gave.

    ``notes`` and ``truncated`` are carried out rather than logged and dropped.
    A caller deciding whether a day was worked at the office needs the
    difference between "nothing was found" and "the search gave up early".
    """
    items: list[dict] = []
    notes: list[str] = []
    total: int | None = None
    offset = 0
    truncated = False

    for _ in range(max_pages):
        page = call(tool, limit=PAGE_LIMIT, offset=offset, **arguments)
        items.extend(page.items)
        notes.extend(page.notes)
        if page.total is not None:
            total = page.total
        if page.next_offset is None:
            break
        if page.next_offset <= offset:
            raise M365Error(f"{tool}: pagination did not advance past offset {offset}")
        offset = page.next_offset
    else:
        truncated = True
        logger.error("m365: %s gave more than %d pages; the answer is partial", tool, max_pages)

    for note in notes:
        logger.warning("m365: %s said: %s", tool, note[:200])
    return SearchResult(items=items, notes=notes, total=total, truncated=truncated)


def tools() -> list[dict]:
    _handshake()
    return _rpc("tools/list", {})["tools"]


def _coerce(value: str):
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def main(argv: list[str]) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    command = argv[1] if len(argv) > 1 else "help"
    if command == "login":
        token(allow_device_code=True)
        who = call("get_me").items[0]
        print(f"signed in as {who.get('displayName')} ({who.get('jobTitle')})")
        print(f"token cache: {_cache_path()}  -- this file is a credential")
        return 0
    if command == "tools":
        for tool in tools():
            schema = tool.get("inputSchema", {})
            print(f"{tool['name']}\n    params: {', '.join(schema.get('properties', {}))}")
        return 0
    if command == "call" and len(argv) > 2:
        kwargs = {k: _coerce(v) for k, v in (a.split("=", 1) for a in argv[3:])}
        for item in call(argv[2], **kwargs).items:
            print(json.dumps(item))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
