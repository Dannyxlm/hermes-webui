"""Focused contracts for profile-explicit Desktop session projection."""

import json

from unittest import mock

import api.models as models
from api.workspace import find_registered_workspace_for_profile_home


def _desktop_row(*, sid="desktop-1", archived=False, cwd=None):
    return {
        "id": sid,
        "source": "desktop",
        "title": "Desktop chat",
        "model": "test/model",
        "message_count": 2,
        "actual_message_count": 2,
        "actual_user_message_count": 1,
        "started_at": 100.0,
        "last_activity": 101.0,
        "archived": archived,
        "cwd": cwd,
    }


def _project_desktop(tmp_path, row, *, sidecar_archived=None, source_filter="desktop"):
    db = tmp_path / "state.db"
    db.touch(exist_ok=True)
    explicit_workspace = tmp_path / "explicit-workspace"
    explicit_workspace.mkdir(exist_ok=True)
    with (
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch("api.models.read_desktop_session_rows", return_value=[row]),
        mock.patch("api.models.read_importable_agent_session_rows", return_value=[]),
        mock.patch(
            "api.models.get_last_workspace_for_profile_home",
            return_value=str(explicit_workspace),
        ),
        mock.patch(
            "api.models.load_workspaces_for_profile_home",
            return_value=[{"path": str(explicit_workspace), "name": "Explicit"}],
        ),
        mock.patch("api.models.find_registered_workspace_for_profile_home", return_value=None),
        mock.patch("api.models.load_projects", return_value=[]),
        mock.patch(
            "api.models._state_projection_sidecar_metadata",
            return_value={"title": None, "archived": sidecar_archived},
        ),
    ):
        return models._load_cli_sessions_uncached(
            tmp_path,
            db,
            _cli_profile="default",
            source_filter=source_filter,
            include_claude_code=False,
            cron_project_limit=False,
            webhook_project_limit=False,
        )


def test_remote_registered_workspace_is_resolved_on_target_not_webui_host(tmp_path):
    profile_home = tmp_path / "profiles" / "atlas"
    profile_home.mkdir(parents=True)
    terminal_cfg = {"backend": "ssh", "cwd": "/srv/cloudseed"}
    workspaces = [
        {"path": "/srv/cloudseed", "name": "CloudSeed"},
        {"path": "/srv/cloudseed/apps/dannyos", "name": "DannyOS"},
    ]

    with mock.patch(
        "api.config.get_config_for_profile_home",
        return_value={"terminal": terminal_cfg},
    ):
        matched = find_registered_workspace_for_profile_home(
            profile_home,
            "/srv/cloudseed/apps/dannyos/api/routes",
            workspaces=workspaces,
        )
        outside = find_registered_workspace_for_profile_home(
            profile_home,
            "/srv/other-project",
            workspaces=workspaces,
        )

    assert matched == "/srv/cloudseed/apps/dannyos"
    assert outside is None


def test_explicit_profile_projection_never_uses_ambient_active_workspace(tmp_path):
    row = _desktop_row(cwd="/target-only/project")
    db = tmp_path / "state.db"
    db.touch()
    profile_home = tmp_path / "profiles" / "default"
    profile_home.mkdir(parents=True)

    with (
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch("api.models.read_desktop_session_rows", return_value=[row]),
        mock.patch(
            "api.models.get_last_workspace",
            side_effect=AssertionError("ambient active workspace must not be read"),
        ),
        mock.patch(
            "api.models.get_last_workspace_for_profile_home",
            return_value="/target-only",
        ) as explicit_last,
        mock.patch(
            "api.models.load_workspaces_for_profile_home",
            return_value=[{"path": "/target-only", "name": "Target"}],
        ) as explicit_spaces,
        mock.patch(
            "api.models.find_registered_workspace_for_profile_home",
            return_value=None,
        ),
        mock.patch("api.models.load_projects", return_value=[]),
        mock.patch(
            "api.models._state_projection_sidecar_metadata",
            return_value={"title": None, "archived": None},
        ),
    ):
        rows = models._load_cli_sessions_uncached(
            profile_home,
            db,
            _cli_profile="default",
            source_filter="desktop",
            include_claude_code=False,
        )

    assert rows[0]["workspace"] == "/target-only"
    explicit_last.assert_called_once_with(profile_home)
    explicit_spaces.assert_called_once_with(profile_home)


def test_native_archive_state_survives_without_sidecar(tmp_path):
    rows = _project_desktop(
        tmp_path,
        _desktop_row(archived=True),
        sidecar_archived=None,
    )

    assert rows[0]["archived"] is True


def test_explicit_sidecar_archive_state_overrides_native_state(tmp_path):
    rows = _project_desktop(
        tmp_path,
        _desktop_row(archived=True),
        sidecar_archived=False,
    )

    assert rows[0]["archived"] is False


def test_unfiltered_desktop_rows_use_same_projector_as_filtered_rows(tmp_path):
    row = _desktop_row(archived=True)

    filtered = _project_desktop(
        tmp_path,
        row,
        sidecar_archived=None,
        source_filter="desktop",
    )
    aggregate = _project_desktop(
        tmp_path,
        row,
        sidecar_archived=None,
        source_filter=None,
    )

    assert aggregate == filtered


def test_legacy_named_profile_project_is_inferred_without_persisting(tmp_path):
    profile_home = tmp_path / "profiles" / "nuru"
    profile_home.mkdir(parents=True)
    db = profile_home / "state.db"
    db.touch()
    workspace = tmp_path / "Nuru Workspace"
    workspace.mkdir()
    session_index = tmp_path / "session-index.json"
    session_index.write_text(
        json.dumps([{"project_id": "legacy-project", "profile": "nuru"}]),
        encoding="utf-8",
    )
    legacy_project = {
        "project_id": "legacy-project",
        "name": "Legacy Nuru",
        "workspace": str(workspace),
    }

    with (
        mock.patch.object(models, "SESSION_INDEX_FILE", session_index),
        mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        mock.patch(
            "api.models.read_desktop_session_rows",
            return_value=[_desktop_row(cwd=str(workspace))],
        ),
        mock.patch(
            "api.models.get_last_workspace_for_profile_home",
            side_effect=AssertionError("registered workspace should win"),
        ),
        mock.patch(
            "api.models.load_workspaces_for_profile_home",
            return_value=[{"path": str(workspace), "name": "Nuru"}],
        ),
        mock.patch(
            "api.models.find_registered_workspace_for_profile_home",
            return_value=str(workspace),
        ),
        mock.patch("api.models.load_projects", return_value=[legacy_project]) as read_projects,
        mock.patch(
            "api.models.save_projects",
            side_effect=AssertionError("listing must not persist project migration"),
        ),
        mock.patch(
            "api.models._state_projection_sidecar_metadata",
            return_value={"title": None, "archived": None},
        ),
    ):
        rows = models._load_cli_sessions_uncached(
            profile_home,
            db,
            _cli_profile="nuru",
            source_filter="desktop",
            include_claude_code=False,
            cron_project_limit=False,
            webhook_project_limit=False,
        )

    assert rows[0]["project_id"] == "legacy-project"
    assert "profile" not in legacy_project
    read_projects.assert_called_once_with(_migrate=False)
