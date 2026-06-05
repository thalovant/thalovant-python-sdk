"""Identity loading for Thalovant HiveMind clients."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .errors import ThalovantIdentityError


_MISSING = object()


@dataclass(frozen=True)
class ThalovantIdentity:
    """HiveMind identity material provisioned by Thalovant."""

    access_key: str
    password: str
    default_master: str
    site_id: str
    default_port: int = 5679
    default_path: str = ""
    crypto_key: str | None = None

    @classmethod
    def from_file(cls, path: str | Path) -> "ThalovantIdentity":
        """Load identity material from a JSON file."""

        identity_path = Path(path).expanduser()
        try:
            raw = json.loads(identity_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ThalovantIdentityError(f"Unable to read identity file: {identity_path}") from exc
        except json.JSONDecodeError as exc:
            raise ThalovantIdentityError(f"Identity file is not valid JSON: {identity_path}") from exc

        if not isinstance(raw, Mapping):
            raise ThalovantIdentityError("Identity file must contain a JSON object.")
        return cls.from_mapping(raw)

    @classmethod
    def from_env(cls, prefix: str = "THALOVANT_") -> "ThalovantIdentity":
        """Load identity material from environment variables."""

        env = os.environ
        return cls.from_mapping(
            {
                "access_key": env.get(f"{prefix}ACCESS_KEY"),
                "password": env.get(f"{prefix}PASSWORD"),
                "crypto_key": env.get(f"{prefix}CRYPTO_KEY"),
                "site_id": env.get(f"{prefix}SITE_ID"),
                "default_master": env.get(f"{prefix}HUB_HTTP_HOST")
                or env.get(f"{prefix}DEFAULT_MASTER"),
                "default_port": env.get(f"{prefix}HUB_HTTP_PORT")
                or env.get(f"{prefix}DEFAULT_PORT"),
                "default_path": env.get(f"{prefix}HUB_HTTP_PATH")
                or env.get(f"{prefix}DEFAULT_PATH"),
            }
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ThalovantIdentity":
        """Normalize identity material from a Thalovant/HiveMind mapping."""

        access_key = _required_string(values, "access_key", aliases=("key", "api_key"))
        password = _required_string(values, "password")
        default_master = _required_string(
            values,
            "default_master",
            aliases=("host", "hub_http_host", "master"),
        )
        site_id = _required_string(values, "site_id", aliases=("siteId", "site"))
        default_port = _int_value(values, "default_port", aliases=("port", "hub_http_port"))
        default_path = _optional_string(
            values,
            "default_path",
            aliases=("defaultPath", "hub_http_path", "path", "uri_path"),
        )
        crypto_key = _optional_string(values, "crypto_key", aliases=("cryptoKey",))

        return cls(
            access_key=access_key,
            password=password,
            default_master=default_master.rstrip("/"),
            default_port=default_port or 5679,
            default_path=_normalize_path(default_path),
            site_id=site_id,
            crypto_key=crypto_key,
        )

    def endpoint_base(self) -> str:
        """Return the HTTP endpoint base URL including port and optional path."""

        master = self.default_master.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
        parsed = urlsplit(master)
        if parsed.scheme and parsed.netloc:
            netloc = parsed.netloc
            if ":" not in netloc.rsplit("@", 1)[-1]:
                netloc = f"{netloc}:{self.default_port}"
            path = "/".join(
                part.strip("/")
                for part in (parsed.path, self.default_path)
                if part and part.strip("/")
            )
            return urlunsplit((parsed.scheme, netloc, f"/{path}" if path else "", "", ""))
        return f"{master.rstrip('/')}:{self.default_port}{self.default_path}"

    def as_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        """Return a serializable identity summary."""

        data: dict[str, Any] = {
            "site_id": self.site_id,
            "default_master": self.default_master,
            "default_port": self.default_port,
            "default_path": self.default_path,
        }
        if include_secrets:
            data.update(
                {
                    "access_key": self.access_key,
                    "password": self.password,
                    "crypto_key": self.crypto_key,
                }
            )
        return data


def _value(values: Mapping[str, Any], key: str, aliases: tuple[str, ...] = ()) -> Any:
    for candidate in (key, *aliases):
        value = values.get(candidate, _MISSING)
        if value is not _MISSING:
            return value
    return _MISSING


def _required_string(values: Mapping[str, Any], key: str, aliases: tuple[str, ...] = ()) -> str:
    value = _optional_string(values, key, aliases=aliases)
    if value is None:
        accepted = ", ".join((key, *aliases))
        raise ThalovantIdentityError(f"Missing required identity field: {accepted}")
    return value


def _optional_string(
    values: Mapping[str, Any],
    key: str,
    aliases: tuple[str, ...] = (),
) -> str | None:
    value = _value(values, key, aliases)
    if value is _MISSING or value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _int_value(values: Mapping[str, Any], key: str, aliases: tuple[str, ...] = ()) -> int | None:
    value = _value(values, key, aliases)
    if value is _MISSING or value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        accepted = ", ".join((key, *aliases))
        raise ThalovantIdentityError(f"Identity field must be an integer: {accepted}") from exc


def _normalize_path(path: str | None) -> str:
    if not path:
        return ""
    normalized = "/" + path.strip("/")
    return "" if normalized == "/" else normalized
