from __future__ import annotations

from dataclasses import dataclass

import pytest

from btc_short_horizon.live.authentication import (
    EXPECTED_CLOB_V2_SDK_VERSION,
    ClobClientSettings,
    LiveCredentials,
    SignatureType,
    assert_live_sdk_version,
    build_live_clob_client,
)


PRIVATE_KEY = "0x" + ("1" * 64)
SIGNER = "0x" + ("2" * 40)
FUNDER = "0x" + ("3" * 40)


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "POLY_PRIVATE_KEY": PRIVATE_KEY,
        "POLY_API_KEY": "api-key-value",
        "POLY_API_SECRET": "api-secret-value",
        "POLY_PASSPHRASE": "api-passphrase-value",
        "POLY_FUNDER": FUNDER,
        "POLY_SIGNATURE_TYPE": "2",
    }
    values.update(overrides)
    return values


def test_credentials_load_explicit_environment_without_leaking_secrets() -> None:
    credentials = LiveCredentials.from_environment(_environment())

    assert credentials.funder == FUNDER
    assert credentials.signature_type is SignatureType.GNOSIS_SAFE
    rendered = repr(credentials)
    assert PRIVATE_KEY not in rendered
    assert "api-key-value" not in rendered
    assert "api-secret-value" not in rendered
    assert "api-passphrase-value" not in rendered
    assert credentials.user_channel_auth() == {
        "apiKey": "api-key-value",
        "secret": "api-secret-value",
        "passphrase": "api-passphrase-value",
    }


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("POLY_PRIVATE_KEY", "", "POLY_PRIVATE_KEY"),
        ("POLY_PRIVATE_KEY", "not-a-key", "private key"),
        ("POLY_FUNDER", "0x1234", "funder"),
        ("POLY_SIGNATURE_TYPE", "4", "signature type"),
        ("POLY_API_SECRET", "secret\nsecond-line", "control characters"),
    ],
)
def test_credentials_reject_missing_or_malformed_values(
    name: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        LiveCredentials.from_environment(_environment(**{name: value}))


def test_credentials_do_not_implicitly_derive_or_create_api_keys() -> None:
    environment = _environment()
    del environment["POLY_API_KEY"]

    with pytest.raises(ValueError, match="POLY_API_KEY"):
        LiveCredentials.from_environment(environment)


def test_credentials_load_secret_files_without_putting_values_in_environment(
    tmp_path,
) -> None:
    values = _environment()
    environment = {
        "POLY_FUNDER": values["POLY_FUNDER"],
        "POLY_SIGNATURE_TYPE": values["POLY_SIGNATURE_TYPE"],
    }
    for name in (
        "POLY_PRIVATE_KEY",
        "POLY_API_KEY",
        "POLY_API_SECRET",
        "POLY_PASSPHRASE",
    ):
        path = (tmp_path / name.casefold()).resolve()
        path.write_text(values[name] + "\n", encoding="utf-8")
        environment[f"{name}_FILE"] = str(path)

    credentials = LiveCredentials.from_environment(environment)

    assert credentials.private_key == PRIVATE_KEY
    assert credentials.user_channel_auth()["apiKey"] == "api-key-value"
    assert PRIVATE_KEY not in repr(credentials)


def test_credentials_reject_ambiguous_or_relative_secret_files(tmp_path) -> None:
    secret_path = (tmp_path / "private-key").resolve()
    secret_path.write_text(PRIVATE_KEY, encoding="utf-8")

    with pytest.raises(ValueError, match="both"):
        LiveCredentials.from_environment(_environment(POLY_PRIVATE_KEY_FILE=str(secret_path)))

    environment = _environment()
    del environment["POLY_PRIVATE_KEY"]
    environment["POLY_PRIVATE_KEY_FILE"] = "relative-secret"
    with pytest.raises(ValueError, match="absolute"):
        LiveCredentials.from_environment(environment)


def test_sdk_version_check_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "btc_short_horizon.live.authentication.package_version",
        lambda _name: "1.0.3",
    )

    with pytest.raises(RuntimeError, match=EXPECTED_CLOB_V2_SDK_VERSION):
        assert_live_sdk_version()


@dataclass
class _FakeApiCreds:
    api_key: str
    api_secret: str
    api_passphrase: str


class _FakeClient:
    address = SIGNER

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def get_address(self) -> str:
        return self.address


def test_client_builder_disables_sdk_post_retries_and_server_time_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "btc_short_horizon.live.authentication.package_version",
        lambda _name: EXPECTED_CLOB_V2_SDK_VERSION,
    )
    credentials = LiveCredentials.from_environment(_environment())

    client = build_live_clob_client(
        credentials,
        settings=ClobClientSettings(),
        client_factory=_FakeClient,
        api_creds_factory=_FakeApiCreds,
    )

    assert client.kwargs == {
        "host": "https://clob.polymarket.com",
        "chain_id": 137,
        "key": PRIVATE_KEY,
        "creds": _FakeApiCreds(
            "api-key-value",
            "api-secret-value",
            "api-passphrase-value",
        ),
        "signature_type": 2,
        "funder": FUNDER,
        "use_server_time": False,
        "retry_on_error": False,
    }


def test_eoa_identity_must_match_the_configured_funder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "btc_short_horizon.live.authentication.package_version",
        lambda _name: EXPECTED_CLOB_V2_SDK_VERSION,
    )
    credentials = LiveCredentials.from_environment(
        _environment(POLY_SIGNATURE_TYPE="0", POLY_FUNDER=FUNDER)
    )

    with pytest.raises(ValueError, match="EOA funder does not match signer"):
        build_live_clob_client(
            credentials,
            client_factory=_FakeClient,
            api_creds_factory=_FakeApiCreds,
        )


@pytest.mark.parametrize(
    "host",
    [
        "http://clob.polymarket.com",
        "https://clob.polymarket.com/path",
        "https://attacker.example",
        "",
    ],
)
def test_live_client_settings_require_a_bare_https_origin(host: str) -> None:
    with pytest.raises(ValueError, match="official CLOB HTTPS origin"):
        ClobClientSettings(host=host)
