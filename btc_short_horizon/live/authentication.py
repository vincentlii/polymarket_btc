"""Explicit, redacted credential loading for the pinned CLOB V2 client."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from importlib.metadata import PackageNotFoundError, version as package_version
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit


EXPECTED_CLOB_V2_SDK_VERSION = "1.0.2"
_SDK_DISTRIBUTION = "py-clob-client-v2"
_PRIVATE_KEY = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_OFFICIAL_CLOB_ORIGIN = "https://clob.polymarket.com"


class SignatureType(IntEnum):
    EOA = 0
    POLY_PROXY = 1
    GNOSIS_SAFE = 2
    POLY_1271 = 3


@dataclass(frozen=True, slots=True)
class ClobClientSettings:
    host: str = "https://clob.polymarket.com"
    chain_id: int = 137

    def __post_init__(self) -> None:
        if not isinstance(self.host, str):
            raise ValueError("CLOB host must be the official CLOB HTTPS origin")
        parsed = urlsplit(self.host)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "clob.polymarket.com"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("CLOB host must be the official CLOB HTTPS origin")
        if isinstance(self.chain_id, bool) or self.chain_id != 137:
            raise ValueError("live Polymarket CLOB chain_id must be 137")
        object.__setattr__(self, "host", _OFFICIAL_CLOB_ORIGIN)


@dataclass(frozen=True, slots=True)
class LiveCredentials:
    """Secrets stay explicit and are excluded from repr by construction."""

    private_key: str = field(repr=False)
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    api_passphrase: str = field(repr=False)
    funder: str
    signature_type: SignatureType

    def __post_init__(self) -> None:
        private_key = _required_secret(self.private_key, "POLY_PRIVATE_KEY")
        if not _PRIVATE_KEY.fullmatch(private_key):
            raise ValueError("private key must contain exactly 32 bytes of hex")
        api_key = _required_secret(self.api_key, "POLY_API_KEY")
        api_secret = _required_secret(self.api_secret, "POLY_API_SECRET")
        api_passphrase = _required_secret(self.api_passphrase, "POLY_PASSPHRASE")
        if not isinstance(self.funder, str) or not _ADDRESS.fullmatch(self.funder.strip()):
            raise ValueError("funder must be a 0x-prefixed 20-byte address")
        try:
            signature_type = SignatureType(self.signature_type)
        except (TypeError, ValueError) as exc:
            raise ValueError("signature type must be one of 0, 1, 2, or 3") from exc
        object.__setattr__(self, "private_key", private_key)
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "api_secret", api_secret)
        object.__setattr__(self, "api_passphrase", api_passphrase)
        object.__setattr__(self, "funder", self.funder.strip())
        object.__setattr__(self, "signature_type", signature_type)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> LiveCredentials:
        source = os.environ if environment is None else environment
        secret_names = (
            "POLY_PRIVATE_KEY",
            "POLY_API_KEY",
            "POLY_API_SECRET",
            "POLY_PASSPHRASE",
        )
        secrets = {name: _environment_secret(source, name) for name in secret_names}
        required = ("POLY_FUNDER", "POLY_SIGNATURE_TYPE")
        missing = [
            name for name in required if not isinstance(source.get(name), str) or not source[name]
        ]
        if missing:
            raise ValueError(f"missing required live environment value: {missing[0]}")
        raw_signature_type = source["POLY_SIGNATURE_TYPE"]
        try:
            signature_type = SignatureType(int(raw_signature_type))
        except (TypeError, ValueError) as exc:
            raise ValueError("signature type must be one of 0, 1, 2, or 3") from exc
        return cls(
            private_key=secrets["POLY_PRIVATE_KEY"],
            api_key=secrets["POLY_API_KEY"],
            api_secret=secrets["POLY_API_SECRET"],
            api_passphrase=secrets["POLY_PASSPHRASE"],
            funder=source["POLY_FUNDER"],
            signature_type=signature_type,
        )

    def user_channel_auth(self) -> dict[str, str]:
        """Create the short-lived subscription fragment; callers must not persist it."""

        return {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "passphrase": self.api_passphrase,
        }


def assert_live_sdk_version() -> str:
    try:
        installed = package_version(_SDK_DISTRIBUTION)
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "Install the project's live dependency group before canary use."
        ) from exc
    if installed != EXPECTED_CLOB_V2_SDK_VERSION:
        raise RuntimeError(
            f"unsupported {_SDK_DISTRIBUTION} version {installed!r}; "
            f"expected {EXPECTED_CLOB_V2_SDK_VERSION}"
        )
    return installed


def build_live_clob_client(
    credentials: LiveCredentials,
    *,
    settings: ClobClientSettings = ClobClientSettings(),
    client_factory: Callable[..., Any] | None = None,
    api_creds_factory: Callable[..., Any] | None = None,
) -> Any:
    """Construct the official SDK without credential derivation or POST retries."""

    assert_live_sdk_version()
    if client_factory is None or api_creds_factory is None:
        try:
            from py_clob_client_v2 import ApiCreds, ClobClient
        except ImportError as exc:  # pragma: no cover - guarded by version check
            raise RuntimeError(
                "Install the project's live dependency group before canary use."
            ) from exc
        client_factory = client_factory or ClobClient
        api_creds_factory = api_creds_factory or ApiCreds
    api_credentials = api_creds_factory(
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        api_passphrase=credentials.api_passphrase,
    )
    client = client_factory(
        host=settings.host,
        chain_id=settings.chain_id,
        key=credentials.private_key,
        creds=api_credentials,
        signature_type=int(credentials.signature_type),
        funder=credentials.funder,
        use_server_time=False,
        retry_on_error=False,
    )
    signer_address = client.get_address()
    if not isinstance(signer_address, str) or not _ADDRESS.fullmatch(signer_address):
        raise ValueError("CLOB SDK returned an invalid signer address")
    if (
        credentials.signature_type is SignatureType.EOA
        and signer_address.casefold() != credentials.funder.casefold()
    ):
        raise ValueError("EOA funder does not match signer address")
    return client


def _required_secret(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    if len(value) > 4096:
        raise ValueError(f"{name} is unreasonably large")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _environment_secret(source: Mapping[str, str], name: str) -> str:
    direct = source.get(name)
    file_value = source.get(f"{name}_FILE")
    has_direct = isinstance(direct, str) and bool(direct)
    has_file = isinstance(file_value, str) and bool(file_value)
    if has_direct and has_file:
        raise ValueError(f"{name} and {name}_FILE cannot both be set")
    if has_direct:
        return _required_secret(direct, name)
    if not has_file:
        raise ValueError(f"missing required live environment value: {name} or {name}_FILE")
    assert isinstance(file_value, str)
    path = Path(file_value)
    if not path.is_absolute():
        raise ValueError(f"{name}_FILE must be an absolute path")
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{name}_FILE must identify a regular non-symlink file")
    if path.stat().st_size > 4098:
        raise ValueError(f"{name}_FILE is unreasonably large")
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{name}_FILE could not be read as UTF-8") from exc
    value = value.removesuffix("\n").removesuffix("\r")
    return _required_secret(value, name)


__all__ = [
    "EXPECTED_CLOB_V2_SDK_VERSION",
    "ClobClientSettings",
    "LiveCredentials",
    "SignatureType",
    "assert_live_sdk_version",
    "build_live_clob_client",
]
