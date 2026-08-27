"""Regression coverage for preserving native Hermes session workspaces in WebUI."""

import json
import time
import sqlite3
import sys
from types import SimpleNamespace
from unittest import mock

import api.models as models
import api.agent_sessions as agent_sessions
from api.agent_sessions import (
    is_cli_session_row,
    normalize_agent_session_source,
    read_desktop_session_rows,
    read_importable_agent_session_rows,
)


def _make_desktop_state_db(
    path,
    *,
    cwd,
    source="desktop",
    with_index=True,
    archived=False,
):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL,
            message_count INTEGER,
            cwd TEXT,
            archived INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        """
    )
    if with_index:
        conn.execute(
            "CREATE INDEX idx_messages_session ON messages(session_id, timestamp)"
        )
    now = time.time()
    conn.execute(
        """
        INSERT INTO sessions (
            id, source, session_source, title, model, started_at, message_count, cwd,
            archived
        ) VALUES (?, ?, 'other', 'Desktop chat', 'test/model', ?, 2, ?, ?)
        """,
        (
            "desktop-session",
            source,
            now,
            str(cwd) if cwd is not None else None,
            int(bool(archived)),
        ),
    )
    conn.executemany(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, 'desktop-session', ?, ?, ?)",
        [
            ("m1", "user", "hello", now + 1),
            ("m2", "assistant", "hi", now + 2),
        ],
    )
    conn.commit()
    conn.close()


def test_desktop_source_is_a_labeled_local_interactive_session():
    source = normalize_agent_session_source("desktop")

    assert source == {
        "raw_source": "desktop",
        "session_source": "cli",
        "source_label": "Desktop",
    }
    assert is_cli_session_row({"source": "desktop", **source}) is True


def test_desktop_reader_reuses_native_logical_projection_and_closes(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    db_path.write_text("", encoding="utf-8")
    seen = {}

    class FakeSessionDB:
        def __init__(self, *, db_path, read_only=False):
            seen["db_path"] = db_path
            seen["read_only"] = read_only

        def list_sessions_rich(self, **kwargs):
            seen["kwargs"] = kwargs
            return [{
                "id": "desktop-tip",
                "source": "desktop",
                "title": "Native Desktop chat",
                "message_count": 4,
                "started_at": time.time() - 10.0,
                "last_active": time.time() - 5.0,
                "cwd": None,
                "archived": True,
                "_lineage_root_id": "desktop-root",
            }]

        def close(self):
            seen["closed"] = True

    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=FakeSessionDB))

    rows = read_desktop_session_rows(db_path)

    assert seen["db_path"] == db_path
    assert seen["read_only"] is True
    assert seen["kwargs"] == {
        "source": "desktop",
        "limit": 15,
        "offset": 0,
        "order_by_last_active": True,
        "compact_rows": True,
        "include_children": False,
        "include_archived": True,
        "min_message_count": 1,
    }
    assert seen["closed"] is True
    assert len(rows) == 1
    assert rows[0]["id"] == "desktop-tip"
    assert rows[0]["source"] == "desktop"
    assert rows[0]["title"] == "Native Desktop chat"
    assert rows[0]["message_count"] == 4
    assert rows[0]["archived"] is True
    assert rows[0]["_lineage_root_id"] == "desktop-root"
    assert rows[0]["raw_source"] == "desktop"
    assert rows[0]["actual_message_count"] == 4
    assert rows[0]["actual_user_message_count"] is None
    assert rows[0]["_lineage_tip_id"] == "desktop-tip"
    assert rows[0]["session_source"] == "cli"
    assert rows[0]["source_label"] == "Desktop"
    assert rows[0]["last_activity"] == rows[0]["last_active"]
    assert rows[0]["last_activity"] > time.time() - 60


def test_desktop_reader_clamps_requested_page_limit(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    db_path.write_text("", encoding="utf-8")
    seen = {}

    class FakeSessionDB:
        def __init__(self, *, db_path, read_only=False):
            pass

        def list_sessions_rich(self, **kwargs):
            seen.update(kwargs)
            return []

        def close(self):
            seen["closed"] = True

    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=FakeSessionDB))

    assert read_desktop_session_rows(db_path, limit=10_000) == []
    assert seen["limit"] == 15
    assert seen["offset"] == 0
    assert seen["closed"] is True


def test_desktop_reader_closes_native_handle_before_compatibility_fallback(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    db_path.write_text("", encoding="utf-8")
    seen = {"closed": False}

    class BrokenSessionDB:
        def __init__(self, *, db_path, read_only=False):
            pass

        def list_sessions_rich(self, **kwargs):
            raise TypeError("got an unexpected keyword argument 'include_archived'")

        def close(self):
            seen["closed"] = True

    def compatibility_reader(*args, **kwargs):
        assert seen["closed"] is True
        seen["compatibility_kwargs"] = kwargs
        return [{"id": "compat-desktop", "started_at": time.time(), "last_activity": time.time()}]

    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=BrokenSessionDB))
    monkeypatch.setattr(agent_sessions, "read_importable_agent_session_rows", compatibility_reader)

    rows = read_desktop_session_rows(db_path)
    assert [r["id"] for r in rows] == ["compat-desktop"]
    assert seen["compatibility_kwargs"]["limit"] == 15
    assert seen["compatibility_kwargs"]["strict_read_only"] is True


def test_desktop_reader_operational_failure_does_not_launch_fallback(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "state.db"
    db_path.write_text("", encoding="utf-8")
    seen = {"closed": False}

    class FailingSessionDB:
        def __init__(self, *, db_path, read_only=False):
            pass

        def list_sessions_rich(self, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            seen["closed"] = True

    def unexpected_fallback(*args, **kwargs):
        raise AssertionError("operational failures must not launch a second scan")

    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=FailingSessionDB))
    monkeypatch.setattr(
        agent_sessions,
        "read_importable_agent_session_rows",
        unexpected_fallback,
    )

    assert read_desktop_session_rows(db_path) == []
    assert seen["closed"] is True


def test_desktop_reader_internal_type_error_does_not_launch_fallback(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "state.db"
    db_path.write_text("", encoding="utf-8")
    seen = {"closed": False}

    class FailingSessionDB:
        def __init__(self, *, db_path, read_only=False):
            pass

        def list_sessions_rich(self, **kwargs):
            raise TypeError("native row decoding failed")

        def close(self):
            seen["closed"] = True

    def unexpected_fallback(*args, **kwargs):
        raise AssertionError("internal TypeError must not launch a second scan")

    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=FailingSessionDB))
    monkeypatch.setattr(
        agent_sessions,
        "read_importable_agent_session_rows",
        unexpected_fallback,
    )

    assert read_desktop_session_rows(db_path) == []
    assert seen["closed"] is True


def test_desktop_compatibility_reader_leaves_missing_index_database_unchanged(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "state.db"
    _make_desktop_state_db(db_path, cwd=tmp_path, with_index=False)
    before = db_path.read_bytes()

    class LegacySessionDB:
        def __init__(self, *, db_path, read_only=False):
            pass

        def list_sessions_rich(self, **kwargs):
            raise TypeError("got an unexpected keyword argument 'include_archived'")

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=LegacySessionDB))

    rows = read_desktop_session_rows(db_path)

    assert [row["id"] for row in rows] == ["desktop-session"]
    assert db_path.read_bytes() == before
    with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True) as verify:
        indexes = {row[1] for row in verify.execute("PRAGMA index_list(messages)")}
    assert "idx_messages_session" not in indexes


def test_strict_reader_never_reopens_writable_when_read_only_open_fails(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "state.db"
    db_path.write_text("not opened", encoding="utf-8")
    attempts = []

    def fail_read_only_open(path, *args, **kwargs):
        attempts.append((str(path), dict(kwargs)))
        if kwargs.get("uri") is True:
            raise sqlite3.OperationalError("read-only open failed")
        raise AssertionError("strict read path must never retry with a writable handle")

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", fail_read_only_open)

    rows = read_importable_agent_session_rows(
        db_path,
        limit=20,
        exclude_sources=None,
        include_sources=("desktop",),
        strict_read_only=True,
    )

    assert rows == []
    assert len(attempts) == 1
    assert attempts[0][1] == {"uri": True}
    assert attempts[0][0].endswith("?mode=ro")


def test_desktop_compatibility_reader_preserves_native_archive_state(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "state.db"
    _make_desktop_state_db(db_path, cwd=tmp_path, archived=True)

    class LegacySessionDB:
        def __init__(self, *, db_path, read_only=False):
            pass

        def list_sessions_rich(self, **kwargs):
            raise TypeError("got an unexpected keyword argument 'include_archived'")

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=LegacySessionDB))

    rows = read_desktop_session_rows(db_path)

    assert rows[0]["archived"] == 1


def test_importable_reader_preserves_native_session_cwd(tmp_path):
    workspace = tmp_path / "CloudSeed Strategy"
    workspace.mkdir()
    db = tmp_path / "state.db"
    _make_desktop_state_db(db, cwd=workspace)

    rows = read_importable_agent_session_rows(
        db,
        limit=20,
        exclude_sources=None,
        include_sources=("desktop",),
    )

    assert len(rows) == 1
    assert rows[0]["cwd"] == str(workspace)


def test_desktop_projection_uses_registered_native_workspace(tmp_path):
    fallback = tmp_path / "Home"
    fallback.mkdir()
    workspace = fallback / "Seed Your Business"
    workspace.mkdir()
    working_directory = workspace / "campaign"
    working_directory.mkdir()
    db = tmp_path / "state.db"
    _make_desktop_state_db(db, cwd=working_directory)

    with (
        mock.patch(
            "api.models.read_desktop_session_rows",
            side_effect=lambda path, log=None: read_importable_agent_session_rows(
                path,
                limit=None,
                log=log,
                exclude_sources=None,
                include_sources=("desktop",),
            ),
        ) as read_rows,
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch(
            "api.models.get_last_workspace_for_profile_home",
            return_value=str(fallback),
        ) as read_last_workspace,
        mock.patch(
            "api.models.load_workspaces_for_profile_home",
            return_value=[
                {"path": str(fallback), "name": "Home"},
                {"path": str(workspace), "name": "Seed Your Business"},
                {"path": "\x00", "name": "Broken Space"},
            ],
        ) as read_workspaces,
        mock.patch(
            "api.models.load_projects",
            return_value=[
                {
                    "project_id": "nuru-syb-project",
                    "name": "Seed Your Business",
                    "profile": "nuru",
                    "workspace": str(workspace),
                },
                {
                    "project_id": "syb-project",
                    "name": "Seed Your Business",
                    "profile": "default",
                    "workspace": str(workspace),
                },
            ],
        ) as read_projects,
        mock.patch("api.models.Session.load_metadata_only", return_value=None),
    ):
        rows = models._load_cli_sessions_uncached(
            tmp_path,
            db,
            _cli_profile="default",
            source_filter="desktop",
            include_claude_code=False,
        )

    assert len(rows) == 1
    read_rows.assert_called_once()
    assert read_rows.call_args.args[0] == db
    read_last_workspace.assert_not_called()
    read_workspaces.assert_called_once_with(tmp_path)
    read_projects.assert_called_once_with(_migrate=False)
    assert rows[0]["workspace"] == str(workspace)
    assert rows[0]["project_id"] == "syb-project"


def test_desktop_projection_falls_back_for_missing_or_unregistered_cwd(tmp_path):
    registered = tmp_path / "Registered"
    registered.mkdir()
    # Exercise the dangerous case: the display fallback itself is also a
    # curated Project workspace. Missing/unregistered cwd must still remain
    # unassigned rather than inheriting this Project ID.
    fallback = registered
    outside = tmp_path / "Outside"
    outside.mkdir()

    for index, cwd in enumerate((None, outside)):
        db = tmp_path / f"state-{index}.db"
        _make_desktop_state_db(db, cwd=cwd)
        with (
            mock.patch("api.models.get_claude_code_sessions", return_value=[]),
            mock.patch(
                "api.models.get_last_workspace_for_profile_home",
                return_value=str(fallback),
            ) as read_last_workspace,
            mock.patch(
                "api.models.load_workspaces_for_profile_home",
                return_value=[{"path": str(registered), "name": "Registered"}],
            ) as read_workspaces,
            mock.patch(
                "api.models.load_projects",
                return_value=[{
                    "project_id": "registered-project",
                    "name": "Registered",
                    "profile": "default",
                    "workspace": str(registered),
                }],
            ),
            mock.patch("api.models.Session.load_metadata_only", return_value=None),
        ):
            rows = models._load_cli_sessions_uncached(
                tmp_path,
                db,
                _cli_profile="default",
                source_filter="desktop",
                include_claude_code=False,
            )

        assert len(rows) == 1
        read_last_workspace.assert_called_once_with(tmp_path)
        if cwd is None:
            read_workspaces.assert_not_called()
        else:
            read_workspaces.assert_called_once_with(tmp_path)
        assert rows[0]["workspace"] == str(fallback)
        assert rows[0]["project_id"] is None


def test_non_desktop_projection_ignores_native_cwd(tmp_path):
    fallback = tmp_path / "Home"
    fallback.mkdir()
    workspace = fallback / "Seed Your Business"
    workspace.mkdir()
    db = tmp_path / "state.db"
    _make_desktop_state_db(db, cwd=workspace, source="telegram")

    with (
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch(
            "api.models.get_last_workspace_for_profile_home",
            return_value=str(fallback),
        ) as read_last_workspace,
        mock.patch(
            "api.models.load_workspaces_for_profile_home",
            side_effect=AssertionError(
                "non-desktop rows must not load the Space registry"
            ),
        ),
        mock.patch(
            "api.models.load_projects",
            side_effect=AssertionError("non-desktop rows must not load project mappings"),
        ),
        mock.patch("api.models.Session.load_metadata_only", return_value=None),
    ):
        rows = models._load_cli_sessions_uncached(
            tmp_path,
            db,
            _cli_profile="default",
            include_desktop_history=False,
            include_claude_code=False,
        )

    assert len(rows) == 1
    read_last_workspace.assert_called_once_with(tmp_path)
    assert rows[0]["workspace"] == str(fallback)
    assert rows[0]["project_id"] is None


def test_unfiltered_projection_keeps_the_bounded_desktop_page(monkeypatch, tmp_path):
    import api.models as models

    fallback = tmp_path / "Home"
    fallback.mkdir()
    workspace = tmp_path / "CloudSeed Strategy"
    workspace.mkdir()
    db = tmp_path / "state.db"
    db.write_text("", encoding="utf-8")
    calls = []
    desktop_calls = []

    desktop_row = {
        "id": "desktop-older",
        "title": "Older Desktop chat",
        "model": "test/model",
        "source": "desktop",
        "raw_source": "desktop",
        "message_count": 2,
        "actual_message_count": 2,
        "actual_user_message_count": 1,
        "last_activity": 10.0,
        "started_at": 9.0,
        "cwd": str(workspace),
    }
    null_cwd_rows = [
        {**desktop_row, "id": f"desktop-unassigned-{index:02d}", "cwd": None}
        for index in range(24)
    ]

    def fake_read_rows(_db_path, **kwargs):
        calls.append(kwargs)
        return []

    def fake_desktop_rows(_db_path, log=None):
        desktop_calls.append((_db_path, log))
        return [desktop_row, *null_cwd_rows]

    monkeypatch.setattr(models, "read_importable_agent_session_rows", fake_read_rows)
    monkeypatch.setattr(models, "read_desktop_session_rows", fake_desktop_rows)
    monkeypatch.setattr(
        models,
        "get_last_workspace_for_profile_home",
        lambda profile_home: fallback if profile_home == tmp_path else None,
    )
    monkeypatch.setattr(
        models,
        "load_workspaces_for_profile_home",
        lambda profile_home: (
            [{"path": str(workspace), "name": "CloudSeed Strategy"}]
            if profile_home == tmp_path
            else []
        ),
    )
    monkeypatch.setattr(
        models,
        "load_projects",
        lambda **_kwargs: [{
            "project_id": "cloudseed-project",
            "name": "CloudSeed Strategy",
            "profile": "default",
            "workspace": str(workspace),
        }],
    )
    monkeypatch.setattr(models.Session, "load_metadata_only", lambda _sid: None)

    rows = models._load_cli_sessions_uncached(
        tmp_path,
        db,
        _cli_profile="default",
        cron_project_limit=False,
        webhook_project_limit=False,
        include_claude_code=False,
    )

    assert [call.get("include_sources") for call in calls] == [None]
    assert calls[0]["exclude_sources"] == ("cron", "webhook", "desktop")
    assert [call[0] for call in desktop_calls] == [db]
    assert [row["session_id"] for row in rows] == [
        "desktop-older",
        *[f"desktop-unassigned-{index:02d}" for index in range(24)],
    ]
    assert rows[0]["workspace"] == str(workspace)
    assert rows[0]["project_id"] == "cloudseed-project"
    assert all(row["workspace"] == str(fallback) for row in rows[1:])
    assert all(row["project_id"] is None for row in rows[1:])


def test_cross_profile_desktop_projection_reads_target_profile_workspace_state(
    monkeypatch,
    tmp_path,
):
    import api.profiles as profiles

    active_home = tmp_path / "hermes"
    target_home = active_home / "profiles" / "nuru"
    target_state = target_home / "webui_state"
    active_home.mkdir()
    target_state.mkdir(parents=True)
    active_workspace = tmp_path / "Active Workspace"
    target_workspace = tmp_path / "Target Workspace"
    target_child = target_workspace / "campaign"
    active_workspace.mkdir()
    target_child.mkdir(parents=True)
    (target_state / "last_workspace.txt").write_text(
        str(target_workspace),
        encoding="utf-8",
    )
    (target_state / "workspaces.json").write_text(
        json.dumps([{"path": str(target_workspace), "name": "Target"}]),
        encoding="utf-8",
    )
    db = target_home / "state.db"
    _make_desktop_state_db(db, cwd=target_child)

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        models,
        "read_desktop_session_rows",
        lambda path, log=None: read_importable_agent_session_rows(
            path,
            limit=None,
            log=log,
            exclude_sources=None,
            include_sources=("desktop",),
        ),
    )
    monkeypatch.setattr(
        models,
        "load_projects",
        lambda **_kwargs: [{
            "project_id": "target-project",
            "name": "Target",
            "profile": "nuru",
            "workspace": str(target_workspace),
        }],
    )
    monkeypatch.setattr(models.Session, "load_metadata_only", lambda _sid: None)

    rows = models._load_cli_sessions_uncached(
        target_home,
        db,
        _cli_profile="nuru",
        source_filter="desktop",
        include_claude_code=False,
    )

    assert len(rows) == 1
    assert rows[0]["profile"] == "nuru"
    assert rows[0]["workspace"] == str(target_workspace)
    assert rows[0]["project_id"] == "target-project"
    assert rows[0]["workspace"] != str(active_workspace)


def test_active_renamed_root_uses_global_workspace_state(monkeypatch, tmp_path):
    import api.profiles as profiles
    import api.workspace as workspace_state

    root_home = tmp_path / "hermes"
    global_state = tmp_path / "global-webui"
    fallback = tmp_path / "Home"
    registered = tmp_path / "Registered Space"
    working_directory = registered / "campaign"
    root_home.mkdir()
    global_state.mkdir()
    fallback.mkdir()
    working_directory.mkdir(parents=True)
    (global_state / "last_workspace.txt").write_text(str(fallback), encoding="utf-8")
    (global_state / "workspaces.json").write_text(
        json.dumps([{"path": str(registered), "name": "Registered"}]),
        encoding="utf-8",
    )
    db = root_home / "state.db"
    _make_desktop_state_db(db, cwd=working_directory)

    monkeypatch.setattr(workspace_state, "_GLOBAL_WS_FILE", global_state / "workspaces.json")
    monkeypatch.setattr(workspace_state, "_GLOBAL_LW_FILE", global_state / "last_workspace.txt")
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "kinni")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: root_home)
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: root_home if name in {"default", "kinni"} else None,
    )
    monkeypatch.setattr(
        profiles,
        "_is_root_profile",
        lambda name: name in {"default", "kinni"},
    )
    monkeypatch.setattr(
        models,
        "read_desktop_session_rows",
        lambda path, log=None: read_importable_agent_session_rows(
            path,
            limit=None,
            log=log,
            exclude_sources=None,
            include_sources=("desktop",),
        ),
    )
    monkeypatch.setattr(
        models,
        "load_projects",
        lambda **_kwargs: [{
            "project_id": "root-project",
            "name": "Registered",
            "profile": "default",
            "workspace": str(registered),
        }],
    )
    monkeypatch.setattr(models.Session, "load_metadata_only", lambda _sid: None)

    rows = models._load_cli_sessions_uncached(
        root_home,
        db,
        _cli_profile="kinni",
        source_filter="desktop",
        include_claude_code=False,
    )

    assert len(rows) == 1
    assert rows[0]["workspace"] == str(registered)
    assert rows[0]["project_id"] == "root-project"
    assert not (root_home / "webui_state").exists()


def test_desktop_import_uses_server_resolved_workspace_and_project(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "desktop-trusted-import"
    workspace = tmp_path / "Trusted Workspace"
    workspace.mkdir()
    messages = [{"role": "user", "content": "hello"}]
    metadata = {
        "session_id": sid,
        "title": "Desktop chat",
        "model": "test/model",
        "profile": "nuru",
        "source_tag": "desktop",
        "raw_source": "desktop",
        "session_source": "cli",
        "source_label": "Desktop",
        "workspace": str(workspace),
        "project_id": "trusted-project",
        "read_only": False,
    }
    captured = {}

    class ImportedSession:
        def __init__(self):
            self.session_id = sid
            self.title = "Desktop chat"
            self.profile = "nuru"
            self.model = "test/model"

        def save(self, touch_updated_at=False):
            captured["saved"] = touch_updated_at

        def compact(self):
            return {
                "session_id": self.session_id,
                "workspace": captured["kwargs"]["workspace"],
                "project_id": captured["kwargs"]["project_id"],
            }

    def fake_import(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return ImportedSession()

    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, _sid: None))
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "bad", lambda _handler, msg, status=400: {"error": msg, "status": status})
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(routes, "_resolve_cli_import_metadata", lambda *args, **kwargs: metadata)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda _sid, profile=None: messages)
    monkeypatch.setattr(routes, "_is_subagent_child_session_id", lambda _sid: False)
    monkeypatch.setattr(routes, "import_cli_session", fake_import)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "_queue_generated_title_for_imported_session", lambda *args: None)

    response = routes._handle_session_import_cli(
        object(),
        {
            "session_id": sid,
            "workspace": "/client-supplied/lie",
            "project_id": "client-supplied-project",
        },
    )

    assert captured["kwargs"]["workspace"] == str(workspace)
    assert captured["kwargs"]["project_id"] == "trusted-project"
    assert captured["saved"] is False
    assert response["session"]["workspace"] == str(workspace)
    assert response["session"]["project_id"] == "trusted-project"


def test_desktop_refresh_preserves_existing_ui_owned_workspace_and_project(monkeypatch):
    import api.routes as routes

    sid = "desktop-existing-sidecar"
    messages = [{"role": "user", "content": "hello"}]

    class ExistingSession:
        def __init__(self):
            self.session_id = sid
            self.profile = "default"
            self.source_tag = "desktop"
            self.raw_source = "desktop"
            self.session_source = "cli"
            self.source_label = "Desktop"
            self.parent_session_id = None
            self.workspace = "/user/chosen/workspace"
            self.project_id = "user-chosen-project"
            self.messages = list(messages)
            self.is_cli_session = True
            self.read_only = False

        def save(self, touch_updated_at=False):
            raise AssertionError("unchanged existing sidecar must not be rewritten")

        def compact(self):
            return {
                "session_id": self.session_id,
                "workspace": self.workspace,
                "project_id": self.project_id,
            }

    existing = ExistingSession()
    projected = {
        "profile": "default",
        "source_tag": "desktop",
        "raw_source": "desktop",
        "session_source": "cli",
        "source_label": "Desktop",
        "workspace": "/server/projected/workspace",
        "project_id": "server-projected-project",
    }
    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, _sid: existing))
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "bad", lambda _handler, msg, status=400: {"error": msg, "status": status})
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *args: True)
    monkeypatch.setattr(routes, "_resolve_cli_import_metadata", lambda *args, **kwargs: projected)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda _sid, profile=None: list(messages))
    monkeypatch.setattr(routes, "_is_subagent_child_session_id", lambda _sid: False)

    response = routes._handle_session_import_cli(object(), {"session_id": sid})

    assert response["imported"] is False
    assert response["session"]["workspace"] == "/user/chosen/workspace"
    assert response["session"]["project_id"] == "user-chosen-project"
