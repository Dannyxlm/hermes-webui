"""Hermex U5 Desktop sidebar / session-open / watcher performance contracts.

Ports the proven live overlay contracts into supported source tests:

- bounded sessions-table-only Desktop projection (newest 15, ≤7 days)
- no messages JOIN / GROUP BY on the fast path
- unrelated non-Desktop rows ignored
- lineage / compression tip collapse without scanning messages
- unfiltered session-open metadata lookup forced onto Desktop-only reader
- watcher fingerprint/projection never touch messages
- watcher starts on first SSE subscriber, does no DB work without subscribers,
  retires after idle grace and releases the thread slot
"""
from __future__ import annotations

import queue
import sqlite3
import threading
import time
from pathlib import Path

import pytest


def _build_db(root: Path) -> Path:
    db = root / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT,
            model TEXT,
            message_count INTEGER DEFAULT 0,
            started_at REAL,
            last_activity_at REAL,
            ended_at REAL,
            end_reason TEXT,
            parent_session_id TEXT,
            archived INTEGER DEFAULT 0,
            hidden INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0,
            model_config TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        """
    )
    now = time.time()
    for index in range(20):
        activity = now - index * 60
        conn.execute(
            "INSERT INTO sessions VALUES (?, 'desktop', ?, 'm', ?, ?, ?, NULL, NULL, NULL, 0, 0, 0, NULL)",
            (f"desktop-{index:02d}", f"Desktop {index}", index + 1, activity, activity),
        )
    # Compression lineage: root + tip. Only tip should surface as the logical row.
    root_ts = now - 30
    tip_ts = now - 10
    conn.execute(
        "INSERT INTO sessions VALUES ('desktop-root', 'desktop', 'Root', 'm', 2, ?, ?, ?, 'compression', NULL, 0, 0, 0, NULL)",
        (root_ts, root_ts, root_ts + 1),
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('desktop-tip', 'desktop', 'Tip', 'm', 5, ?, ?, NULL, NULL, 'desktop-root', 0, 0, 0, NULL)",
        (tip_ts, tip_ts),
    )
    # Older than 7 days — must be excluded.
    conn.execute(
        "INSERT INTO sessions VALUES ('desktop-old', 'desktop', 'Old Desktop', 'm', 3, ?, ?, NULL, NULL, NULL, 0, 0, 0, NULL)",
        (now - 10 * 24 * 60 * 60, now - 10 * 24 * 60 * 60),
    )
    # Hot non-Desktop noise with heavy messages — must never appear or be scanned.
    conn.execute(
        "INSERT INTO sessions VALUES ('telegram-hot', 'telegram', 'Telegram', 'm', 999, ?, ?, NULL, NULL, NULL, 0, 0, 0, NULL)",
        (now + 1000, now + 1000),
    )
    for index in range(100):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('telegram-hot', 'user', 'noise', ?)",
            (now + index,),
        )
    # Tool-heavy child under a desktop parent should not become its own logical root.
    conn.execute(
        "INSERT INTO sessions VALUES ('desktop-tool', 'tool', 'Tool child', 'm', 8, ?, ?, NULL, NULL, 'desktop-00', 0, 0, 0, NULL)",
        (now + 5, now + 5),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def desktop_db(tmp_path):
    return _build_db(tmp_path)


def _sql_trace(db_path: Path):
    """Open a traced connection helper used by production open_state_db_readonly patches."""
    statements: list[str] = []

    def open_traced(path, log=None):
        del log
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
        conn.set_trace_callback(statements.append)
        return conn

    return statements, open_traced


def test_bounded_desktop_projection_is_recent_desktop_only_and_never_reads_messages(
    desktop_db, monkeypatch
):
    import api.agent_sessions as agent_sessions

    statements, open_traced = _sql_trace(desktop_db)
    monkeypatch.setattr(agent_sessions, "open_state_db_readonly", open_traced)

    rows = agent_sessions.read_desktop_session_rows(desktop_db)

    ids = [row["id"] for row in rows]
    assert len(rows) <= 15
    assert "telegram-hot" not in ids
    assert "desktop-old" not in ids
    assert "desktop-tool" not in ids
    assert all(row.get("source") == "desktop" for row in rows)
    # Compression lineage collapses onto the tip id as the visible logical session.
    assert "desktop-tip" in ids
    assert "desktop-root" not in ids

    joined = "\n".join(statements).lower()
    assert "from messages" not in joined
    assert "join messages" not in joined
    assert "group by" not in joined


def test_bounded_desktop_projection_prefers_newest_fifteen(desktop_db, monkeypatch):
    import api.agent_sessions as agent_sessions

    monkeypatch.setattr(
        agent_sessions,
        "read_importable_agent_session_rows",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("fast path must not fall back to generic messages projection")
        ),
    )

    rows = agent_sessions.read_desktop_session_rows(desktop_db)
    assert len(rows) == 15
    # Newest pure desktop row is desktop-00; lineage tip is also very recent.
    assert rows[0]["id"] in {"desktop-00", "desktop-tip"}
    assert "desktop-19" not in {r["id"] for r in rows}


def test_unfiltered_get_cli_sessions_forces_desktop_only_reader(tmp_path, monkeypatch):
    """Session-open metadata lookup calls get_cli_sessions() with no filter.

    On Desktop deployments that must stay cheap, that unfiltered call is forced
    onto the Desktop-only reader and must not scan Claude Code / generic rows.
    """
    import api.models as models

    seen = {}

    def fake_load(hermes_home, db_path, _cli_profile, source_filter=None, **kwargs):
        seen["source_filter"] = source_filter
        seen["include_claude_code"] = kwargs.get("include_claude_code", True)
        return [{"session_id": "desktop-00", "source_tag": "desktop"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", fake_load)
    monkeypatch.setattr(
        models,
        "_resolve_cli_sessions_context",
        lambda source_filter=None, **kwargs: (
            tmp_path,
            tmp_path / "state.db",
            "default",
            ("cache", source_filter or "", kwargs.get("include_claude_code", True)),
        ),
    )
    monkeypatch.setattr(models, "_cli_sessions_cache_ttl_seconds", lambda: 0)
    (tmp_path / "state.db").touch()

    rows = models.get_cli_sessions()  # unfiltered session-open path

    assert seen["source_filter"] == "desktop"
    assert seen["include_claude_code"] is False
    assert rows and rows[0]["session_id"] == "desktop-00"


def test_watcher_fingerprint_is_desktop_only_and_never_reads_messages(desktop_db, monkeypatch):
    import api.agent_sessions as agent_sessions
    import api.gateway_watcher as gateway_watcher

    statements, open_traced = _sql_trace(desktop_db)
    monkeypatch.setattr(agent_sessions, "open_state_db_readonly", open_traced)
    monkeypatch.setattr(gateway_watcher, "open_state_db_readonly", open_traced)

    first = gateway_watcher._cheap_change_fingerprint(desktop_db)
    assert isinstance(first, str)

    conn = sqlite3.connect(desktop_db)
    conn.execute("UPDATE sessions SET title = 'Telegram changed' WHERE id = 'telegram-hot'")
    conn.execute("UPDATE sessions SET title = 'Outside cap changed' WHERE id = 'desktop-19'")
    conn.commit()
    conn.close()
    assert first == gateway_watcher._cheap_change_fingerprint(desktop_db)

    conn = sqlite3.connect(desktop_db)
    conn.execute("UPDATE sessions SET title = 'Visible changed' WHERE id = 'desktop-00'")
    conn.commit()
    conn.close()
    assert first != gateway_watcher._cheap_change_fingerprint(desktop_db)

    joined = "\n".join(statements).lower()
    assert "from messages" not in joined
    assert "join messages" not in joined


def test_watcher_projection_bypasses_native_recursive_messages_projection(
    desktop_db, monkeypatch
):
    import api.agent_sessions as agent_sessions
    import api.gateway_watcher as gateway_watcher

    statements, open_traced = _sql_trace(desktop_db)
    monkeypatch.setattr(agent_sessions, "open_state_db_readonly", open_traced)
    native_calls = []

    def forbidden_native(*args, **kwargs):
        native_calls.append((args, kwargs))
        raise AssertionError("watcher must not call the recursive native projection")

    monkeypatch.setattr(agent_sessions, "read_importable_agent_session_rows", forbidden_native)
    # Even if native SessionDB is present, watcher must stay on sessions-table path.
    monkeypatch.setattr(
        agent_sessions,
        "read_desktop_session_rows",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("watcher uses its own bounded reader, not request fallback")
        )
        if False
        else agent_sessions.read_desktop_session_rows(*a, **k),
    )

    sessions = gateway_watcher._get_agent_sessions_from_db(desktop_db)
    fingerprint = gateway_watcher._cheap_change_fingerprint(desktop_db)

    assert native_calls == []
    assert len(sessions) == 15
    assert isinstance(fingerprint, str)
    assert all(row["source"] == "desktop" for row in sessions)
    assert "telegram-hot" not in {row["session_id"] for row in sessions}
    assert "desktop-old" not in {row["session_id"] for row in sessions}
    joined = "\n".join(statements).lower()
    assert "from messages" not in joined
    assert "join messages" not in joined


def test_idle_watcher_does_no_database_work_and_exits_after_grace(desktop_db, monkeypatch):
    import api.gateway_watcher as gateway_watcher

    monkeypatch.setattr(gateway_watcher, "WATCHER_IDLE_GRACE_SECONDS", 0.05)
    calls = {"fingerprint": 0, "projection": 0}
    monkeypatch.setattr(
        gateway_watcher,
        "_cheap_change_fingerprint",
        lambda _path: calls.__setitem__("fingerprint", calls["fingerprint"] + 1) or "fp",
    )
    monkeypatch.setattr(
        gateway_watcher,
        "_get_agent_sessions_from_db",
        lambda _path=None: calls.__setitem__("projection", calls["projection"] + 1) or [],
    )

    watcher = gateway_watcher.GatewayWatcher(state_db_path=desktop_db)
    watcher.POLL_INTERVAL = 0.02
    watcher.start()
    watcher._thread.join(timeout=0.5)

    assert watcher.is_alive() is False
    assert calls == {"fingerprint": 0, "projection": 0}


def test_idle_retirement_releases_thread_slot_before_return(desktop_db, monkeypatch):
    import api.gateway_watcher as gateway_watcher

    monkeypatch.setattr(gateway_watcher, "WATCHER_IDLE_GRACE_SECONDS", 0.0)
    watcher = gateway_watcher.GatewayWatcher(state_db_path=desktop_db)
    stale_slot = object()
    watcher._thread = stale_slot
    watcher._poll_loop()
    assert watcher._thread is None


def test_first_subscriber_revives_watcher_and_last_unsubscribe_goes_idle(
    desktop_db, monkeypatch
):
    import api.gateway_watcher as gateway_watcher

    monkeypatch.setattr(gateway_watcher, "WATCHER_IDLE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(gateway_watcher, "_cheap_change_fingerprint", lambda _path: "fp")
    monkeypatch.setattr(gateway_watcher, "_get_agent_sessions_from_db", lambda _path=None: [])

    watcher = gateway_watcher.GatewayWatcher(state_db_path=desktop_db)
    watcher.POLL_INTERVAL = 0.02
    assert watcher.is_alive() is False

    subscriber = watcher.subscribe()
    assert watcher.is_alive() is True

    watcher.unsubscribe(subscriber)
    watcher._thread.join(timeout=0.5)
    assert watcher.is_alive() is False


def test_benchmark_bounded_desktop_reader_stays_subsecond_with_message_noise(
    desktop_db, monkeypatch
):
    """Lightweight timing receipt: sessions-only path must ignore message bloat."""
    import api.agent_sessions as agent_sessions
    import api.gateway_watcher as gateway_watcher

    # Amplify message noise.
    conn = sqlite3.connect(desktop_db)
    now = time.time()
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('telegram-hot', 'assistant', 'x', ?)",
        [(now + i,) for i in range(2000)],
    )
    conn.commit()
    conn.close()

    t0 = time.perf_counter()
    rows = agent_sessions.read_desktop_session_rows(desktop_db)
    t1 = time.perf_counter()
    fp = gateway_watcher._cheap_change_fingerprint(desktop_db)
    t2 = time.perf_counter()
    sessions = gateway_watcher._get_agent_sessions_from_db(desktop_db)
    t3 = time.perf_counter()

    assert len(rows) == 15
    assert isinstance(fp, str)
    assert len(sessions) == 15
    # Generous CI-safe budget; the slow path on multi-GB DBs is seconds+.
    assert (t1 - t0) < 0.5
    assert (t2 - t1) < 0.5
    assert (t3 - t2) < 0.5
