"""Focused integration tests for the release-smoke authentication CLI."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hermex_release_smoke_auth.py"


@pytest.fixture
def isolated_paths(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    state_dir = tmp_path / "state"
    credential_dir = tmp_path / "credentials"
    credential_dir.mkdir(mode=0o700)
    cookie_file = credential_dir / "cookie.jar"
    csrf_file = credential_dir / "csrf.token"
    env = os.environ.copy()
    env["HERMES_WEBUI_STATE_DIR"] = str(state_dir)
    env.pop("HERMES_WEBUI_SESSION_TTL", None)
    env.pop("HERMES_WEBUI_COOKIE_NAME", None)
    return env, state_dir, cookie_file, csrf_file


def run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def create_bundle(
    env: dict[str, str],
    cookie_file: Path,
    csrf_file: Path,
) -> subprocess.CompletedProcess[str]:
    return run_cli(
        env,
        "create",
        "--cookie-file",
        str(cookie_file),
        "--csrf-file",
        str(csrf_file),
        "--ttl-seconds",
        "300",
        "--profile",
        "default",
    )


def cookie_value(cookie_file: Path) -> str:
    records = [
        line
        for line in cookie_file.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(records) == 1
    fields = records[0].split("\t")
    assert len(fields) == 7
    return fields[-1]


def test_create_writes_private_default_profile_bundle_recognized_after_restart(
    isolated_paths: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_dir, cookie_file, csrf_file = isolated_paths
    cookie_file.touch(mode=0o600)
    csrf_file.touch(mode=0o600)
    started_at = time.time()

    result = create_bundle(env, cookie_file, csrf_file)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert stat.S_IMODE(cookie_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(csrf_file.stat().st_mode) == 0o600

    cookie = cookie_value(cookie_file)
    token = cookie.rsplit(".", 1)[0]
    sessions = json.loads((state_dir / ".sessions.json").read_text(encoding="utf-8"))
    record = sessions[token]
    assert record["auth_type"] == "release_smoke"
    assert record["username"] == "codex"
    assert record["bound_profile"] == "default"
    assert started_at + 295 <= record["expiry"] <= time.time() + 305

    verify = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; from api import auth; "
                "lines=[line for line in Path(sys.argv[1]).read_text().splitlines() "
                "if line and not line.startswith('#')]; "
                "cookie=lines[0].split('\\t')[-1]; "
                "csrf=Path(sys.argv[2]).read_text().strip(); "
                "raise SystemExit(0 if auth.verify_session(cookie) "
                "and auth.verify_csrf_token(cookie, csrf) else 1)"
            ),
            str(cookie_file),
            str(csrf_file),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr
    assert verify.stdout == ""


def test_revoke_removes_persisted_session_and_cookie_file(
    isolated_paths: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_dir, cookie_file, csrf_file = isolated_paths
    assert create_bundle(env, cookie_file, csrf_file).returncode == 0
    token = cookie_value(cookie_file).rsplit(".", 1)[0]

    result = run_cli(env, "revoke", "--cookie-file", str(cookie_file))

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert not cookie_file.exists()
    sessions = json.loads((state_dir / ".sessions.json").read_text(encoding="utf-8"))
    assert token not in sessions
    assert csrf_file.exists(), "the caller owns CSRF-file cleanup"


def test_create_rejects_symlink_without_touching_target_or_creating_session(
    isolated_paths: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_dir, cookie_file, csrf_file = isolated_paths
    target = cookie_file.parent / "target"
    target.write_text("do-not-print-or-overwrite", encoding="utf-8")
    target.chmod(0o600)
    cookie_file.symlink_to(target)

    result = create_bundle(env, cookie_file, csrf_file)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "do-not-print-or-overwrite" not in result.stderr
    assert target.read_text(encoding="utf-8") == "do-not-print-or-overwrite"
    assert not csrf_file.exists()
    assert not (state_dir / ".sessions.json").exists()


def test_create_rejects_permissive_existing_file_without_truncating_it(
    isolated_paths: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_dir, cookie_file, csrf_file = isolated_paths
    cookie_file.write_text("do-not-print-or-overwrite", encoding="utf-8")
    cookie_file.chmod(0o644)

    result = create_bundle(env, cookie_file, csrf_file)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "do-not-print-or-overwrite" not in result.stderr
    assert cookie_file.read_text(encoding="utf-8") == "do-not-print-or-overwrite"
    assert stat.S_IMODE(cookie_file.stat().st_mode) == 0o644
    assert not csrf_file.exists()
    assert not (state_dir / ".sessions.json").exists()


def test_create_rejects_nonprivate_credential_directory(
    isolated_paths: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_dir, cookie_file, csrf_file = isolated_paths
    cookie_file.parent.chmod(0o755)

    result = create_bundle(env, cookie_file, csrf_file)

    assert result.returncode != 0
    assert result.stdout == ""
    assert not cookie_file.exists()
    assert not csrf_file.exists()
    assert not (state_dir / ".sessions.json").exists()


@pytest.mark.parametrize(
    ("extra_args", "expected_fragment"),
    [
        (("--ttl-seconds", "59"), "ttl"),
        (("--ttl-seconds", "301"), "ttl"),
        (("--profile", "atlas"), "profile"),
    ],
)
def test_create_rejects_unbounded_or_nondefault_credentials(
    isolated_paths: tuple[dict[str, str], Path, Path, Path],
    extra_args: tuple[str, str],
    expected_fragment: str,
) -> None:
    env, state_dir, cookie_file, csrf_file = isolated_paths
    args = [
        "create",
        "--cookie-file",
        str(cookie_file),
        "--csrf-file",
        str(csrf_file),
        *extra_args,
    ]

    result = run_cli(env, *args)

    assert result.returncode != 0
    assert result.stdout == ""
    assert expected_fragment in result.stderr.lower()
    assert not cookie_file.exists()
    assert not csrf_file.exists()
    assert not (state_dir / ".sessions.json").exists()


def test_revoke_rejects_malformed_secret_without_echoing_or_deleting_it(
    isolated_paths: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, _state_dir, cookie_file, _csrf_file = isolated_paths
    secret = "do-not-print-me"
    cookie_file.write_text(secret, encoding="utf-8")
    cookie_file.chmod(0o600)

    result = run_cli(env, "revoke", "--cookie-file", str(cookie_file))

    assert result.returncode != 0
    assert result.stdout == ""
    assert secret not in result.stderr
    assert cookie_file.read_text(encoding="utf-8") == secret


def test_revoke_rejects_symlink_without_echoing_or_deleting_target(
    isolated_paths: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, _state_dir, cookie_file, _csrf_file = isolated_paths
    target = cookie_file.parent / "target"
    secret = "do-not-print-or-delete"
    target.write_text(secret, encoding="utf-8")
    target.chmod(0o600)
    cookie_file.symlink_to(target)

    result = run_cli(env, "revoke", "--cookie-file", str(cookie_file))

    assert result.returncode != 0
    assert result.stdout == ""
    assert secret not in result.stderr
    assert cookie_file.is_symlink()
    assert target.read_text(encoding="utf-8") == secret
