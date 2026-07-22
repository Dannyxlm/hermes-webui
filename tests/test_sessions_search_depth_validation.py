"""Regression: a malformed/negative ``depth`` on the session content-search
endpoint must not crash or silently exclude the newest messages.

``GET /api/sessions/search?...&depth=<x>`` parsed ``depth`` with a bare
``int()``. A non-numeric value (e.g. ``?depth=deep``) raised ValueError, which
propagated to the top-level request handler and surfaced as a generic HTTP 500.

``depth`` caps how many leading messages are scanned per session
(``sess.messages[:depth]``). A negative value sliced as ``messages[:-n]``,
silently dropping the *most recent* messages from the search instead of capping
the scan — so a match in a session's latest turn would be missed. depth is now
clamped to ``>= 0`` (0 keeps its existing "search the whole transcript"
meaning), mirroring the guard sibling handlers already use.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


def _run_search(query):
    """Invoke _handle_sessions_search against one synthetic session whose match
    lives in its LAST message, capturing the JSON payload/status."""
    import api.routes as routes

    sessions_meta = [{"session_id": "s1", "title": "Untitled", "profile": "default"}]
    session = SimpleNamespace(
        session_id="s1",
        messages=[
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "second message"},
            {"role": "user", "content": "NEEDLE in the latest message"},
        ],
    )
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload

    # The content search reads through get_session_for_scan (the LRU-transparent
    # scan accessor), not get_session — patching the wrong one here would make
    # the depth assertions pass vacuously on an empty result set.
    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.routes.get_session_for_scan", return_value=session
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.j", side_effect=fake_j
    ):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


def test_search_non_numeric_depth_does_not_500():
    # Before the fix this raised ValueError -> 500.
    captured = _run_search("/api/sessions/search?q=needle&content=1&depth=deep")
    assert captured["status"] == 200
    # depth falls back to 5 (>= 3 messages here), so the needle is found.
    assert captured["payload"]["count"] == 1


def test_search_negative_depth_still_scans_newest_message():
    # depth=-2 with 3 messages: an unclamped messages[:-2] scan would look only
    # at the first message and MISS the needle in the latest one. Clamped to a
    # default >= 0, the newest message is searched and the match is found.
    captured = _run_search("/api/sessions/search?q=needle&content=1&depth=-2")
    assert captured["status"] == 200
    assert captured["payload"]["count"] == 1


def test_search_valid_depth_still_caps_scan():
    # depth=1 scans only the first message, which does NOT contain the needle,
    # so no match — proving the cap still works for well-formed input.
    captured = _run_search("/api/sessions/search?q=needle&content=1&depth=1")
    assert captured["status"] == 200
    assert captured["payload"]["count"] == 0


def _run_bounded_search(query, sessions_meta, get_session_for_scan):
    import api.routes as routes

    captured = {}

    def fake_j(_handler, payload, status=200, **_kwargs):
        captured.update(payload=payload, status=status)

    with patch("api.routes.all_sessions", return_value=list(sessions_meta)), patch(
        "api.routes.get_session_for_scan", side_effect=get_session_for_scan
    ), patch("api.profiles.get_active_profile_name", return_value="default"), patch(
        "api.routes.load_settings", return_value={}
    ), patch("api.routes.j", side_effect=fake_j):
        routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured


def test_search_default_candidate_window_only_loads_200_recent_transcripts():
    sessions = [
        {
            "session_id": f"s{index}",
            "title": "Untitled",
            "profile": "default",
            "updated_at": index,
        }
        for index in range(205)
    ]
    loaded = []

    def get_session_for_scan(session_id):
        loaded.append(session_id)
        return SimpleNamespace(messages=[])

    captured = _run_bounded_search(
        "/api/sessions/search?q=needle&content=1",
        sessions,
        get_session_for_scan,
    )
    payload = captured["payload"]

    assert captured["status"] == 200
    assert loaded == [f"s{index}" for index in range(204, 4, -1)]
    assert payload["candidate_count"] == 200
    assert payload["candidate_total"] == 205
    assert payload["candidates_scanned"] == 200
    assert payload["transcripts_scanned"] == 200
    assert payload["has_more_candidates"] is True
    assert payload["partial"] is True


def test_search_default_result_limit_stops_after_50_matches():
    sessions = [
        {
            "session_id": f"s{index}",
            "title": f"needle {index}",
            "profile": "default",
            "updated_at": index,
        }
        for index in range(60)
    ]

    captured = _run_bounded_search(
        "/api/sessions/search?q=needle&content=1",
        sessions,
        lambda _session_id: (_ for _ in ()).throw(AssertionError("title matches need no transcript")),
    )
    payload = captured["payload"]

    assert captured["status"] == 200
    assert payload["count"] == 50
    assert payload["candidate_count"] == 60
    assert payload["candidates_scanned"] == 50
    assert payload["transcripts_scanned"] == 0
    assert payload["result_limit"] == 50
    assert payload["result_limit_reached"] is True
    assert payload["has_more_candidates"] is False
    assert payload["partial"] is True


def test_search_query_limits_are_capped_server_side():
    captured = _run_bounded_search(
        "/api/sessions/search?q=needle&candidate_limit=999999&limit=999999",
        [],
        lambda _session_id: None,
    )

    assert captured["payload"]["candidate_limit"] == 1000
    assert captured["payload"]["result_limit"] == 200
    assert captured["payload"]["partial"] is False
