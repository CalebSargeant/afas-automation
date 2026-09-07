"""The MCP transport: block splitting, pagination and the auth refusal.

All offline. ``_rpc`` is replaced with captured connector answers, so what is
tested is the part that decides what the payload means -- which is the part
that turns a format change into either a loud failure or a quiet wrong claim.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import pathlib
import time
import urllib.error

import pytest

from afas_declaraties import m365_mcp


def block(payload) -> dict:
    return {"type": "text", "text": payload if isinstance(payload, str) else json.dumps(payload)}


def answer(*payloads, is_error: bool = False) -> dict:
    return {"content": [block(p) for p in payloads], "isError": is_error}


@pytest.fixture(autouse=True)
def _no_handshake(monkeypatch):
    monkeypatch.setattr(m365_mcp, "_initialised", True)


def rpc_returning(*answers):
    """Serve one captured answer per call, so pagination can be exercised."""
    remaining = list(answers)
    seen: list[dict] = []

    def _rpc(_method, params):
        seen.append(params.get("arguments", {}))
        return remaining.pop(0)

    _rpc.seen = seen
    return _rpc


# ---------------------------------------------------------------------------
# taking one answer apart
# ---------------------------------------------------------------------------


def test_the_three_block_kinds_are_told_apart(monkeypatch):
    monkeypatch.setattr(
        m365_mcp,
        "_rpc",
        rpc_returning(
            answer(
                {"searchInfo": {"mode": "per_chat_scan"}},
                {"uri": "teams:///chats/a", "id": "1", "summary": "one"},
                {"uri": "teams:///chats/a", "id": "2", "summary": "two"},
                {"moreResults": True, "nextOffset": 2, "totalResultCount": 12},
            )
        ),
    )
    page = m365_mcp.call("chat_message_search", query="*")
    assert [i["id"] for i in page.items] == ["1", "2"]
    assert page.info == {"mode": "per_chat_scan"}
    assert (page.next_offset, page.total) == (2, 12)
    assert page.notes == []


def test_a_single_page_answer_has_no_next_offset(monkeypatch):
    monkeypatch.setattr(
        m365_mcp,
        "_rpc",
        rpc_returning(answer({"uri": "mail:///m/1", "id": "1"}, {"totalResultCount": 1})),
    )
    page = m365_mcp.call("outlook_email_search")
    assert page.next_offset is None
    assert page.total == 1


def test_a_prose_note_is_kept_rather_than_dropped(monkeypatch):
    """The chat search prefixes a note when a scan was cut short. Losing it
    turns a partial answer into an apparently complete one."""
    monkeypatch.setattr(
        m365_mcp,
        "_rpc",
        rpc_returning(
            answer("Note: results are partial, the scan hit its time budget.", {"nextOffset": None})
        ),
    )
    page = m365_mcp.call("chat_message_search", query="*")
    assert page.items == []
    assert "partial" in page.notes[0]


def test_an_unrecognised_block_makes_the_answer_partial(monkeypatch):
    """Skipping it silently would make a moved payload look exactly like a quiet
    week. Raising would let one new metadata block break every run. It becomes a
    note instead, which the calendar reader turns into degraded."""
    monkeypatch.setattr(
        m365_mcp,
        "_rpc",
        rpc_returning(answer({"somethingNew": 1}, {"totalResultCount": 0})),
    )
    page = m365_mcp.call("outlook_calendar_search")
    assert page.items == []
    assert "unrecognised" in page.notes[0]


def test_a_tool_level_error_is_raised_with_its_text(monkeypatch):
    monkeypatch.setattr(
        m365_mcp,
        "_rpc",
        rpc_returning(
            answer("FORBIDDEN: requires the 'Mail.Send' delegated permission", is_error=True)
        ),
    )
    with pytest.raises(m365_mcp.M365Error, match="Mail.Send"):
        m365_mcp.call("outlook_send_mail")


# ---------------------------------------------------------------------------
# paging
# ---------------------------------------------------------------------------


def test_search_follows_next_offset_to_the_end(monkeypatch):
    rpc = rpc_returning(
        answer({"uri": "u", "id": "1"}, {"nextOffset": 1, "totalResultCount": 3}),
        answer({"uri": "u", "id": "2"}, {"nextOffset": 2, "totalResultCount": 3}),
        answer({"uri": "u", "id": "3"}, {"totalResultCount": 3}),
    )
    monkeypatch.setattr(m365_mcp, "_rpc", rpc)
    found = m365_mcp.search("outlook_calendar_search", query="*")
    assert [i["id"] for i in found.items] == ["1", "2", "3"]
    assert [a["offset"] for a in rpc.seen] == [0, 1, 2]
    assert all(a["limit"] == m365_mcp.PAGE_LIMIT for a in rpc.seen)
    assert found.total == 3
    assert found.complete


def test_pagination_that_does_not_advance_raises(monkeypatch):
    """A repeating offset is an infinite loop that would otherwise present as a
    hung nightly job."""
    monkeypatch.setattr(
        m365_mcp,
        "_rpc",
        rpc_returning(
            answer({"uri": "u", "id": "1"}, {"nextOffset": 0}),
            answer({"uri": "u", "id": "1"}, {"nextOffset": 0}),
        ),
    )
    with pytest.raises(m365_mcp.M365Error, match="did not advance"):
        m365_mcp.search("outlook_calendar_search", query="*")


def test_hitting_the_page_cap_marks_the_answer_incomplete(monkeypatch):
    monkeypatch.setattr(
        m365_mcp,
        "_rpc",
        rpc_returning(
            *[answer({"uri": "u", "id": str(n)}, {"nextOffset": n + 1}) for n in range(3)]
        ),
    )
    found = m365_mcp.search("outlook_calendar_search", query="*", max_pages=3)
    assert found.truncated
    assert not found.complete


def test_a_note_on_any_page_makes_the_whole_search_incomplete(monkeypatch):
    monkeypatch.setattr(
        m365_mcp,
        "_rpc",
        rpc_returning(
            answer("Partial: rate limited.", {"nextOffset": 1}),
            answer({"uri": "u", "id": "2"}, {"totalResultCount": 2}),
        ),
    )
    found = m365_mcp.search("chat_message_search", query="*")
    assert found.items and not found.complete


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def jwt(expires_at: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expires_at}).encode()).decode().strip("=")
    return f"header.{payload}.signature"


def test_a_live_cached_token_is_reused(monkeypatch, tmp_path):
    cache = tmp_path / "token.json"
    cache.write_text(json.dumps({"access_token": jwt(time.time() + 3600)}))
    monkeypatch.setattr(m365_mcp, "CACHE", cache)
    monkeypatch.setattr(m365_mcp, "SEED", "")
    monkeypatch.setattr(m365_mcp, "SEED_JSON", "")
    assert m365_mcp.token().startswith("header.")


def test_a_scheduled_run_refuses_to_stop_and_sign_in(monkeypatch, tmp_path):
    """A device-code flow blocks until a human types a code. A CronJob has no
    human, so it must fail immediately and say what to run instead."""
    monkeypatch.setattr(m365_mcp, "CACHE", tmp_path / "absent.json")
    monkeypatch.setattr(m365_mcp, "SEED", "")
    monkeypatch.setattr(m365_mcp, "SEED_JSON", "")
    with pytest.raises(m365_mcp.M365AuthError, match="login"):
        m365_mcp.token()


def test_an_expired_token_is_refreshed_and_the_rotated_one_written(monkeypatch, tmp_path):
    cache = tmp_path / "token.json"
    cache.write_text(json.dumps({"access_token": jwt(time.time() - 10), "refresh_token": "old"}))
    monkeypatch.setattr(m365_mcp, "CACHE", cache)
    monkeypatch.setattr(m365_mcp, "SEED", "")
    monkeypatch.setattr(m365_mcp, "SEED_JSON", "")
    fresh = {"access_token": jwt(time.time() + 3600), "refresh_token": "new"}
    monkeypatch.setattr(m365_mcp, "_post_form", lambda _url, _data: fresh)

    assert m365_mcp.token() == fresh["access_token"]
    # Entra rotates the refresh token on use; not persisting it means the next
    # run replays a spent one.
    assert json.loads(cache.read_text())["refresh_token"] == "new"


def test_a_read_only_secret_is_seeded_into_a_writable_cache(monkeypatch, tmp_path):
    seed = tmp_path / "secret" / "m365.json"
    seed.parent.mkdir()
    seed.write_text(json.dumps({"access_token": jwt(time.time() + 3600)}))
    cache = tmp_path / "run" / "token.json"
    monkeypatch.setattr(m365_mcp, "CACHE", cache)
    monkeypatch.setattr(m365_mcp, "SEED", str(seed))
    monkeypatch.setattr(m365_mcp, "SEED_JSON", "")

    assert m365_mcp.token().startswith("header.")
    assert cache.exists()


def test_the_secret_can_arrive_as_an_env_var_instead_of_a_file(monkeypatch, tmp_path):
    """Every other credential here rides the release Secret through envFrom, so
    the token does too rather than earning a bespoke single-key volume."""
    cache = tmp_path / "run" / "token.json"
    monkeypatch.setattr(m365_mcp, "CACHE", cache)
    monkeypatch.setattr(m365_mcp, "SEED", "")
    monkeypatch.setattr(
        m365_mcp, "SEED_JSON", json.dumps({"access_token": jwt(time.time() + 3600)})
    )

    assert m365_mcp.token().startswith("header.")
    assert cache.exists()


def test_every_request_carries_a_fresh_json_rpc_id(monkeypatch):
    seen = []

    def _urlopen_capture(url, *, data, headers=None, timeout):
        seen.append(json.loads(data)["id"])
        raise m365_mcp.M365Error("stop here")

    monkeypatch.setattr(m365_mcp, "_urlopen", _urlopen_capture)
    monkeypatch.setattr(m365_mcp, "token", lambda **_: "t")
    for _ in range(2):
        with pytest.raises(m365_mcp.M365Error):
            m365_mcp._rpc("tools/call", {})
    assert seen[0] != seen[1]


def test_a_non_https_url_is_refused(monkeypatch):
    """M365_MCP_URL and M365_TENANT both come from the environment, and urllib
    speaks file:// — which would turn a token request into a local file read."""
    with pytest.raises(m365_mcp.M365Error, match="non-https"):
        m365_mcp._urlopen("file:///etc/passwd", data=b"", timeout=1)


# --- transport: the failure paths ------------------------------------------
#
# These are the paths that decide whether a broken connector is loud or silent,
# which makes them the ones worth pinning down. All offline.


def _raise_oserror(*_args, **_kwargs):
    raise OSError("read-only file system")


def raiser(exc):
    def _boom(*_args, **_kwargs):
        raise exc

    return _boom


class FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x.invalid", code, "err", {}, io.BytesIO(body.encode()))


def test_entras_error_body_is_returned_not_discarded(monkeypatch):
    """Entra reports OAuth failures as a 4xx with the reason in the body, so
    throwing the body away loses the only explanation there is."""
    monkeypatch.setattr(
        m365_mcp,
        "_urlopen",
        raiser(http_error(400, '{"error": "invalid_grant", "error_description": "expired"}')),
    )
    assert m365_mcp._post_form("https://x.invalid", {})["error"] == "invalid_grant"


def test_a_non_json_error_body_still_raises_cleanly(monkeypatch):
    monkeypatch.setattr(m365_mcp, "_urlopen", raiser(http_error(502, "<html>gateway</html>")))
    with pytest.raises(m365_mcp.M365Error, match="HTTP 502"):
        m365_mcp._post_form("https://x.invalid", {})


def test_an_unreachable_host_raises_rather_than_returning_nothing(monkeypatch):
    monkeypatch.setattr(m365_mcp, "_urlopen", raiser(urllib.error.URLError("no route")))
    with pytest.raises(m365_mcp.M365Error, match="cannot reach"):
        m365_mcp._post_form("https://x.invalid", {})


def test_a_malformed_access_token_reads_as_expired():
    """Better to refresh needlessly than to send a token that cannot be parsed."""
    assert m365_mcp._expiry("not-a-jwt") == 0.0
    assert m365_mcp._expiry("a.!!!.c") == 0.0


def test_the_cache_is_seeded_from_a_read_only_file(monkeypatch, tmp_path):
    seed = tmp_path / "secret" / "m365.json"
    seed.parent.mkdir()
    seed.write_text('{"access_token": "x"}')
    cache = tmp_path / "run" / "token.json"
    monkeypatch.setattr(m365_mcp, "CACHE", cache)
    monkeypatch.setattr(m365_mcp, "SEED", str(seed))
    monkeypatch.setattr(m365_mcp, "SEED_JSON", "")
    assert m365_mcp._cache_path().read_text() == '{"access_token": "x"}'


def test_an_unwritable_cache_warns_but_does_not_kill_the_run(monkeypatch, tmp_path, caplog):
    """The access token in hand still works; it is the rotated refresh token
    that is lost, and that is a warning for next time, not a failure now."""
    monkeypatch.setattr(m365_mcp, "CACHE", tmp_path / "token.json")
    monkeypatch.setattr(m365_mcp, "SEED", "")
    monkeypatch.setattr(m365_mcp, "SEED_JSON", "")
    monkeypatch.setattr(pathlib.Path, "write_text", _raise_oserror)
    with caplog.at_level(logging.WARNING):
        m365_mcp._save({"access_token": "x"})
    assert "cannot write" in caplog.text


def test_an_unreadable_cache_falls_through_instead_of_crashing(monkeypatch, tmp_path):
    cache = tmp_path / "token.json"
    cache.write_text("{not json")
    monkeypatch.setattr(m365_mcp, "CACHE", cache)
    monkeypatch.setattr(m365_mcp, "SEED", "")
    monkeypatch.setattr(m365_mcp, "SEED_JSON", "")
    with pytest.raises(m365_mcp.M365AuthError):
        m365_mcp.token()


def test_a_rejected_refresh_falls_back_to_a_fresh_sign_in(monkeypatch, tmp_path):
    cache = tmp_path / "token.json"
    cache.write_text(json.dumps({"access_token": jwt(0), "refresh_token": "spent"}))
    monkeypatch.setattr(m365_mcp, "CACHE", cache)
    monkeypatch.setattr(m365_mcp, "SEED", "")
    monkeypatch.setattr(m365_mcp, "SEED_JSON", "")
    monkeypatch.setattr(m365_mcp, "_post_form", lambda *_: {"error": "invalid_grant"})
    monkeypatch.setattr(m365_mcp, "_device_code", lambda: {"access_token": jwt(9e9)})
    assert m365_mcp.token(allow_device_code=True).startswith("header.")


def test_the_device_code_flow_polls_until_the_user_finishes(monkeypatch, capsys):
    answers = [
        {
            "user_code": "ABC-123",
            "verification_uri": "https://x.invalid",
            "device_code": "d",
            "expires_in": 900,
            "interval": 1,
        },
        {"error": "authorization_pending"},
        {"error": "slow_down"},
        {"access_token": jwt(9e9)},
    ]
    monkeypatch.setattr(m365_mcp, "_post_form", lambda *_: answers.pop(0))
    monkeypatch.setattr(m365_mcp.time, "sleep", lambda _s: None)
    assert m365_mcp._device_code()["access_token"].startswith("header.")
    assert "ABC-123" in capsys.readouterr().err


def test_a_refused_device_code_says_why(monkeypatch):
    monkeypatch.setattr(
        m365_mcp,
        "_post_form",
        lambda *_: {"error": "unauthorized_client", "error_description": "not preauthorized"},
    )
    with pytest.raises(m365_mcp.M365AuthError, match="not preauthorized"):
        m365_mcp._device_code()


def test_a_real_sign_in_failure_stops_the_poll(monkeypatch):
    answers = [
        {
            "user_code": "A",
            "verification_uri": "https://x.invalid",
            "device_code": "d",
            "expires_in": 900,
            "interval": 1,
        },
        {"error": "expired_token", "error_description": "the code expired"},
    ]
    monkeypatch.setattr(m365_mcp, "_post_form", lambda *_: answers.pop(0))
    monkeypatch.setattr(m365_mcp.time, "sleep", lambda _s: None)
    with pytest.raises(m365_mcp.M365AuthError, match="expired"):
        m365_mcp._device_code()


def rpc_transport(monkeypatch, body: str):
    monkeypatch.setattr(m365_mcp, "token", lambda **_: "t")
    monkeypatch.setattr(m365_mcp, "_urlopen", lambda *a, **k: FakeResponse(body))


def test_an_sse_framed_answer_is_understood(monkeypatch):
    """The server may answer as text/event-stream even when JSON was acceptable."""
    rpc_transport(monkeypatch, 'event: message\ndata: {"result": {"ok": true}}\n\n')
    assert m365_mcp._rpc("tools/list", {}) == {"ok": True}


def test_a_json_rpc_error_is_raised_with_its_message(monkeypatch):
    rpc_transport(
        monkeypatch, json.dumps({"error": {"code": -32602, "message": "Input validation error"}})
    )
    with pytest.raises(m365_mcp.M365Error, match="Input validation error"):
        m365_mcp._rpc("tools/call", {})


def test_a_non_json_body_is_not_mistaken_for_an_empty_answer(monkeypatch):
    rpc_transport(monkeypatch, "<html>502 Bad Gateway</html>")
    with pytest.raises(m365_mcp.M365Error, match="non-JSON"):
        m365_mcp._rpc("tools/call", {})


def test_an_http_error_from_the_connector_is_raised(monkeypatch):
    monkeypatch.setattr(m365_mcp, "token", lambda **_: "t")
    monkeypatch.setattr(m365_mcp, "_urlopen", raiser(http_error(401, "No valid issuers detected")))
    with pytest.raises(m365_mcp.M365Error, match="401"):
        m365_mcp._rpc("tools/call", {})


def test_an_unreachable_connector_is_raised(monkeypatch):
    monkeypatch.setattr(m365_mcp, "token", lambda **_: "t")
    monkeypatch.setattr(m365_mcp, "_urlopen", raiser(urllib.error.URLError("dns")))
    with pytest.raises(m365_mcp.M365Error, match="cannot reach"):
        m365_mcp._rpc("tools/call", {})


def test_the_handshake_runs_once_per_process(monkeypatch):
    calls = []
    monkeypatch.setattr(m365_mcp, "_initialised", False)
    monkeypatch.setattr(m365_mcp, "_rpc", lambda m, _p: calls.append(m) or {"tools": []})
    m365_mcp.tools()
    m365_mcp.tools()
    assert calls.count("initialize") == 1


def test_cli_arguments_are_coerced_to_their_obvious_types():
    assert m365_mcp._coerce("true") is True
    assert m365_mcp._coerce("false") is False
    assert m365_mcp._coerce("25") == 25
    assert m365_mcp._coerce("2026-09-01") == "2026-09-01"


def test_the_cli_prints_the_tool_list(monkeypatch, capsys):
    monkeypatch.setattr(
        m365_mcp, "tools", lambda: [{"name": "get_me", "inputSchema": {"properties": {}}}]
    )
    assert m365_mcp.main(["m365_mcp", "tools"]) == 0
    assert "get_me" in capsys.readouterr().out


def test_the_cli_prints_one_json_object_per_item(monkeypatch, capsys):
    monkeypatch.setattr(
        m365_mcp, "call", lambda _t, **_k: m365_mcp.Page(items=[{"id": "1"}, {"id": "2"}])
    )
    assert m365_mcp.main(["m365_mcp", "call", "outlook_email_search", "limit=2"]) == 0
    assert capsys.readouterr().out.count('"id"') == 2


def test_the_cli_reports_a_successful_login(monkeypatch, capsys):
    monkeypatch.setattr(m365_mcp, "token", lambda **_: "t")
    monkeypatch.setattr(
        m365_mcp,
        "call",
        lambda _t, **_k: m365_mcp.Page(items=[{"displayName": "The User", "jobTitle": "Engineer"}]),
    )
    assert m365_mcp.main(["m365_mcp", "login"]) == 0
    assert "The User" in capsys.readouterr().out


def test_an_unknown_cli_command_is_a_usage_error(capsys):
    assert m365_mcp.main(["m365_mcp"]) == 2
    assert "login" in capsys.readouterr().out
