from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from patchouli_cli.errors import config_error
from patchouli_cli.secure_fs import read_trusted_file

_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", re.ASCII)
_TOP_LEVEL_KEYS = {"version", "profiles"}
_PROFILE_KEYS = {"endpoint", "api_version"}
_MAX_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    endpoint: str
    api_version: str


def default_config_path(environ: Mapping[str, str]) -> Path:
    if os.name == "nt":
        base = environ.get("APPDATA")
        if base:
            return Path(base) / "PatchouliLib" / "config.toml"
    base = environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "patchouli" / "config.toml"
    return Path.home() / ".config" / "patchouli" / "config.toml"


def default_state_path(environ: Mapping[str, str]) -> Path:
    configured = environ.get("PATCHOULI_STATE_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt":
        base = environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "PatchouliLib" / "operations"
    base = environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "patchouli" / "operations"
    return Path.home() / ".local" / "state" / "patchouli" / "operations"


def resolve_profile(
    *,
    profile_name: str | None,
    config_path: str | None,
    environ: Mapping[str, str],
) -> Profile:
    name = profile_name or environ.get("PATCHOULI_PROFILE", "default")
    if _PROFILE_PATTERN.fullmatch(name) is None:
        raise config_error("profile name must use bounded portable characters")

    path_value = config_path or environ.get("PATCHOULI_CONFIG_FILE")
    path = Path(path_value) if path_value else default_config_path(environ)
    data = _read_config(path, required=bool(path_value))

    profile_data = _profile_data(data, name)
    endpoint_value = environ.get("PATCHOULI_ENDPOINT", profile_data.get("endpoint"))
    version_value = environ.get("PATCHOULI_API_VERSION", profile_data.get("api_version", "v1"))
    if not isinstance(endpoint_value, str):
        raise config_error("profile endpoint is required in config or PATCHOULI_ENDPOINT")
    if not isinstance(version_value, str) or version_value != "v1":
        raise config_error("only the accepted v1 compatibility profile is supported")
    endpoint = _validate_endpoint(endpoint_value)
    return Profile(name=name, endpoint=endpoint, api_version=version_value)


def _read_config(path: Path, *, required: bool) -> dict[str, object]:
    try:
        contents = read_trusted_file(path, max_bytes=_MAX_CONFIG_BYTES, required=required)
    except FileNotFoundError as exc:
        raise config_error("configured profile file does not exist") from exc
    except (OSError, PermissionError, ValueError) as exc:
        raise config_error(
            "profile is not a trusted regular file or its parent is untrusted"
        ) from exc
    if contents is None:
        return {}
    try:
        parsed: object = tomllib.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise config_error("profile file is not readable valid TOML") from exc
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise config_error("profile file must contain a TOML table")
    data = dict(parsed)
    if set(data) - _TOP_LEVEL_KEYS:
        raise config_error("profile file contains unsupported top-level settings")
    version = data.get("version", 1)
    if version != 1:
        raise config_error("profile file version is not supported")
    return data


def _profile_data(data: Mapping[str, object], name: str) -> dict[str, object]:
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict) or not all(isinstance(key, str) for key in profiles):
        raise config_error("profiles must be a TOML table")
    for candidate in profiles.values():
        if not isinstance(candidate, dict) or not all(isinstance(key, str) for key in candidate):
            raise config_error("each profile must be a TOML table")
        if set(candidate) - _PROFILE_KEYS:
            raise config_error("profile file contains unsupported or secret settings")
    selected = profiles.get(name, {})
    if not isinstance(selected, dict):  # validated above; narrows the type for MyPy
        raise config_error("selected profile must be a TOML table")
    result = dict(selected)
    return result


def _validate_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise config_error("profile endpoint must be an HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise config_error("profile endpoint must be an HTTPS origin without user information")
    del port  # accessing it above validates the optional numeric port
    return value.rstrip("/")
