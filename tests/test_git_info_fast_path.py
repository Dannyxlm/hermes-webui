import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "hermex-tests@example.test")
    _git(path, "config", "user.name", "Hermex Tests")
    (path / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "fixture")
    _git(path, "branch", "-M", "main")
    return path


class _Handler:
    command = "GET"
    headers = {}
    client_address = ("127.0.0.1", 12345)

    def __init__(self) -> None:
        self.status = None
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.response_headers.append((name, value))

    def end_headers(self) -> None:
        pass

    def log_message(self, *_args) -> None:
        pass

    def payload(self) -> dict:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_git_info_is_lightweight_for_nested_workspace(tmp_path, monkeypatch):
    from api import workspace_git

    repo = _init_repo(tmp_path / "repo")
    nested = repo / "nested" / "workspace"
    nested.mkdir(parents=True)

    def fail_full_status(*_args, **_kwargs):
        raise AssertionError("the git-info probe must not enumerate full status")

    monkeypatch.setattr(workspace_git, "git_status", fail_full_status)
    original_run_git = workspace_git._run_git
    commands = []

    def capture_run_git(ctx_or_cwd, args, **kwargs):
        commands.append(list(args))
        return original_run_git(ctx_or_cwd, args, **kwargs)

    monkeypatch.setattr(workspace_git, "_run_git", capture_run_git)

    assert workspace_git.git_info(nested) == {
        "branch": "main",
        "is_git": True,
        "repo_root": str(repo.resolve()),
    }
    assert commands == [
        ["rev-parse", "--show-toplevel"],
        ["branch", "--show-current"],
    ]


def test_git_info_supports_linked_worktree_and_non_repo(tmp_path):
    from api.workspace_git import git_info

    repo = _init_repo(tmp_path / "repo")
    worktree = tmp_path / "linked-worktree"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))

    assert (worktree / ".git").is_file()
    assert git_info(worktree) == {
        "branch": "feature",
        "is_git": True,
        "repo_root": str(worktree.resolve()),
    }

    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_info(plain) is None


def test_git_info_preserves_detached_head_as_a_valid_repository(tmp_path):
    from api.workspace_git import git_info

    repo = _init_repo(tmp_path / "repo")
    _git(repo, "checkout", "-q", "--detach")

    assert git_info(repo) == {
        "branch": "",
        "is_git": True,
        "repo_root": str(repo.resolve()),
    }


def test_git_subprocess_environment_overrides_localized_host_locale(monkeypatch):
    from api import workspace_git

    monkeypatch.setenv("LC_ALL", "fr_CA.UTF-8")
    monkeypatch.setenv("LANG", "fr_CA.UTF-8")

    env = workspace_git._clean_git_env({"LC_ALL": "de_DE.UTF-8"})

    assert env["LC_ALL"] == "C"


def test_resolve_git_context_propagates_non_repo_discovery_failures(tmp_path, monkeypatch):
    from api import workspace_git

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fail_discovery(_cwd, args, **kwargs):
        assert args == ["rev-parse", "--show-toplevel"]
        assert kwargs.get("check") is True
        raise workspace_git.GitWorkspaceError("fatal: detected dubious ownership", "git_failed")

    monkeypatch.setattr(workspace_git, "_run_git", fail_discovery)

    with pytest.raises(workspace_git.GitWorkspaceError, match="dubious ownership"):
        workspace_git.resolve_git_context(workspace)


def test_git_info_propagates_branch_probe_failures(tmp_path, monkeypatch):
    from api import workspace_git

    repo = _init_repo(tmp_path / "repo")
    original_run_git = workspace_git._run_git

    def fail_branch(ctx_or_cwd, args, **kwargs):
        if args == ["branch", "--show-current"]:
            assert kwargs.get("check") is True
            raise workspace_git.GitWorkspaceError("branch metadata is unreadable", "git_failed")
        return original_run_git(ctx_or_cwd, args, **kwargs)

    monkeypatch.setattr(workspace_git, "_run_git", fail_branch)

    with pytest.raises(workspace_git.GitWorkspaceError, match="branch metadata is unreadable"):
        workspace_git.git_info(repo)


def test_git_info_route_does_not_call_git_status(tmp_path, monkeypatch):
    from api import routes, workspace_git

    repo = _init_repo(tmp_path / "repo")
    nested = repo / "nested"
    nested.mkdir()
    session_reads = []

    def get_session_metadata_only(_sid, **kwargs):
        session_reads.append(kwargs)
        return SimpleNamespace(workspace=str(nested))

    monkeypatch.setattr(routes, "get_session", get_session_metadata_only)

    def fail_full_status(*_args, **_kwargs):
        raise AssertionError("/api/git-info must not invoke git_status")

    monkeypatch.setattr(workspace_git, "git_status", fail_full_status)

    handler = _Handler()
    routes.handle_get(handler, urlparse("/api/git-info?session_id=session-1"))

    assert handler.status == 200
    assert session_reads
    assert all(read == {"metadata_only": True} for read in session_reads)
    assert handler.payload() == {
        "git": {
            "branch": "main",
            "is_git": True,
            "repo_root": str(repo.resolve()),
        }
    }


def test_workspace_git_badge_uses_only_the_lightweight_git_info_contract():
    source = Path("static/workspace.js").read_text(encoding="utf-8")
    start = source.index("async function _refreshGitBadge()")
    end = source.index("\nfunction navigateUp()", start)
    helper = source[start:end]

    assert "/api/git-info?session_id=" in helper
    assert "g.branch" in helper
    assert "g.dirty" not in helper
    assert "g.ahead" not in helper
    assert "g.behind" not in helper
