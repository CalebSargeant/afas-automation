"""The MCP transport: block splitting, pagination and the auth refusal.

All offline. ``_rpc`` is replaced with captured connector answers, so what is
tested is the part that decides what the payload means -- which is the part
that turns a format change into either a loud failure or a quiet wrong claim.
"""

from __future__ import annotations

import base64
import json
import time

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
