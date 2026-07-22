#!/usr/bin/env python3
"""Create and revoke bounded credentials for a Hermex release smoke test.

The CLI is the supported boundary between release tooling and ``api.auth``.
Credential values are written only to owner-private files, never to stdout or
stderr. ``create`` is intended to run before the Hermex service restart so the
new process loads the persisted session. The live verifier should log out over
HTTP before calling ``revoke`` for best-effort persisted-session cleanup.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Importing WebUI configuration may discover optional Hermes plugins. Missing
# optional plugin dependencies are irrelevant to this leaf auth command and
# must not add noise to its no-credential-output interface.
logging.getLogger("hermes_cli.plugins").setLevel(logging.ERROR)

from api import auth  # noqa: E402 -- scripts execute with scripts/ as sys.path[0]


DEFAULT_TTL_SECONDS = 300
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 300
DEFAULT_PROFILE = "default"
MAX_COOKIE_FILE_BYTES = 4096
_COOKIE_VALUE_RE = re.compile(r"^[0-9a-f]{64}\.[0-9a-f]{64}$")


class ReleaseSmokeAuthError(RuntimeError):
    """A safe, non-secret CLI failure."""


@dataclass(frozen=True)
class CredentialFile:
    path: Path
    fd: int
    info: os.stat_result
    created: bool = False


def _ttl_seconds(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ttl must be an integer") from exc
    if not MIN_TTL_SECONDS <= value <= MAX_TTL_SECONDS:
        raise argparse.ArgumentTypeError(
            f"ttl must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS} seconds"
        )
    return value


def _private_path(path: Path) -> Path:
    if not path.is_absolute():
        raise ReleaseSmokeAuthError("credential paths must be absolute")
    path = Path(os.path.abspath(path))
    try:
        parent_info = os.lstat(path.parent)
    except OSError as exc:
        raise ReleaseSmokeAuthError("credential parent is unavailable") from exc
    if not stat.S_ISDIR(parent_info.st_mode):
        raise ReleaseSmokeAuthError("credential parent must be a real directory")
    if parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
        raise ReleaseSmokeAuthError("credential parent ownership or permissions are unsafe")
    return path


def _open_private_output(path: Path) -> CredentialFile:
    path = _private_path(path)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except OSError as exc:
            raise ReleaseSmokeAuthError("credential output could not be created safely") from exc
    except OSError as exc:
        raise ReleaseSmokeAuthError("credential output could not be opened safely") from exc

    info: os.stat_result | None = None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReleaseSmokeAuthError("credential output must be one regular file")
        if info.st_uid != os.geteuid():
            raise ReleaseSmokeAuthError("credential output ownership is unsafe")
        if created:
            os.fchmod(fd, 0o600)
            info = os.fstat(fd)
        elif stat.S_IMODE(info.st_mode) != 0o600:
            raise ReleaseSmokeAuthError("credential output permissions must be 0600")
        if info.st_size != 0:
            raise ReleaseSmokeAuthError("credential output must be empty")
        if not _path_still_names_file(path, info):
            raise ReleaseSmokeAuthError("credential output changed while opening")
        return CredentialFile(path=path, fd=fd, info=info, created=created)
    except Exception:
        os.close(fd)
        if created:
            _unlink_if_same(path, info)
        raise


def _open_private_input(path: Path) -> CredentialFile:
    path = _private_path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReleaseSmokeAuthError("credential input could not be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReleaseSmokeAuthError("credential input must be one regular file")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ReleaseSmokeAuthError("credential input ownership or permissions are unsafe")
        if not 0 < info.st_size <= MAX_COOKIE_FILE_BYTES:
            raise ReleaseSmokeAuthError("credential input size is invalid")
        if not _path_still_names_file(path, info):
            raise ReleaseSmokeAuthError("credential input changed while opening")
        return CredentialFile(path=path, fd=fd, info=info)
    except Exception:
        os.close(fd)
        raise


def _path_still_names_file(path: Path, expected: os.stat_result) -> bool:
    try:
        current = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_dev == expected.st_dev
        and current.st_ino == expected.st_ino
    )


def _unlink_if_same(path: Path, expected: os.stat_result | None) -> None:
    if expected is None:
        return
    if _path_still_names_file(path, expected):
        try:
            os.unlink(path)
        except OSError:
            pass


def _write_all(fd: int, payload: bytes) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise ReleaseSmokeAuthError("credential output write failed")
        remaining = remaining[written:]
    os.fchmod(fd, 0o600)
    os.fsync(fd)


def _clear_output(fd: int) -> None:
    try:
        os.ftruncate(fd, 0)
        os.fsync(fd)
    except OSError:
        pass


def _persisted_expiry(cookie_value: str) -> float:
    token = cookie_value.rsplit(".", 1)[0]
    persisted = auth._load_sessions()
    record = persisted.get(token)
    expiry = auth._session_expiry(record)
    if expiry is None:
        raise ReleaseSmokeAuthError("release-smoke session was not persisted")
    return expiry


def create_credentials(cookie_file: Path, csrf_file: Path, ttl_seconds: int, profile: str) -> None:
    cookie_handle: CredentialFile | None = None
    csrf_handle: CredentialFile | None = None
    cookie_value: str | None = None
    succeeded = False
    try:
        cookie_handle = _open_private_output(cookie_file)
        csrf_handle = _open_private_output(csrf_file)
        if (cookie_handle.info.st_dev, cookie_handle.info.st_ino) == (
            csrf_handle.info.st_dev,
            csrf_handle.info.st_ino,
        ):
            raise ReleaseSmokeAuthError("cookie and CSRF outputs must be different files")

        previous_ttl = os.environ.get("HERMES_WEBUI_SESSION_TTL")
        os.environ["HERMES_WEBUI_SESSION_TTL"] = str(ttl_seconds)
        try:
            cookie_value = auth.create_session(
                auth_type="release_smoke",
                username="codex",
                bound_profile=profile,
            )
        finally:
            if previous_ttl is None:
                os.environ.pop("HERMES_WEBUI_SESSION_TTL", None)
            else:
                os.environ["HERMES_WEBUI_SESSION_TTL"] = previous_ttl

        expiry = _persisted_expiry(cookie_value)
        csrf_token = auth.csrf_token_for_session(cookie_value)
        if not csrf_token:
            raise ReleaseSmokeAuthError("release-smoke CSRF token was not created")
        cookie_payload = (
            "# Netscape HTTP Cookie File\n"
            f"127.0.0.1\tFALSE\t/\tFALSE\t{int(expiry)}\t"
            f"{auth._resolve_cookie_name()}\t{cookie_value}\n"
        ).encode("utf-8")
        _write_all(cookie_handle.fd, cookie_payload)
        _write_all(csrf_handle.fd, (csrf_token + "\n").encode("utf-8"))
        succeeded = True
    except Exception:
        if cookie_value:
            try:
                auth.invalidate_session(cookie_value)
            except Exception:
                pass
        for handle in (cookie_handle, csrf_handle):
            if handle is not None:
                _clear_output(handle.fd)
        raise
    finally:
        for handle in (cookie_handle, csrf_handle):
            if handle is not None:
                os.close(handle.fd)
        if not succeeded:
            for handle in (cookie_handle, csrf_handle):
                if handle is not None and handle.created:
                    _unlink_if_same(handle.path, handle.info)


def _read_cookie_value(fd: int) -> str:
    info = os.fstat(fd)
    data = bytearray()
    while len(data) < info.st_size:
        chunk = os.read(fd, info.st_size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseSmokeAuthError("credential input is malformed") from exc
    records = [line for line in text.splitlines() if line and not line.startswith("#")]
    if len(records) != 1:
        raise ReleaseSmokeAuthError("credential input is malformed")
    fields = records[0].split("\t")
    if (
        len(fields) != 7
        or fields[0] != "127.0.0.1"
        or fields[1] != "FALSE"
        or fields[2] != "/"
        or fields[3] != "FALSE"
        or not fields[4].isdigit()
        or fields[5] != auth._resolve_cookie_name()
        or not _COOKIE_VALUE_RE.fullmatch(fields[6])
    ):
        raise ReleaseSmokeAuthError("credential input is malformed")
    return fields[6]


def revoke_credentials(cookie_file: Path) -> None:
    handle = _open_private_input(cookie_file)
    try:
        cookie_value = _read_cookie_value(handle.fd)
        token = cookie_value.rsplit(".", 1)[0]
        auth.invalidate_session(cookie_value)
        if token in auth._load_sessions():
            raise ReleaseSmokeAuthError("release-smoke session revocation was not persisted")
        if not _path_still_names_file(handle.path, handle.info):
            raise ReleaseSmokeAuthError("credential input changed during revocation")
        os.unlink(handle.path)
    finally:
        os.close(handle.fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create a bounded release-smoke credential bundle")
    create.add_argument("--cookie-file", required=True, type=Path)
    create.add_argument("--csrf-file", required=True, type=Path)
    create.add_argument("--ttl-seconds", type=_ttl_seconds, default=DEFAULT_TTL_SECONDS)
    create.add_argument("--profile", choices=(DEFAULT_PROFILE,), default=DEFAULT_PROFILE)

    revoke = commands.add_parser("revoke", help="revoke a persisted release-smoke session")
    revoke.add_argument("--cookie-file", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            create_credentials(args.cookie_file, args.csrf_file, args.ttl_seconds, args.profile)
        else:
            revoke_credentials(args.cookie_file)
    except ReleaseSmokeAuthError as exc:
        print(f"hermex_release_smoke_auth: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"hermex_release_smoke_auth: unexpected {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
