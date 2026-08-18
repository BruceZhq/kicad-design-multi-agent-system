from __future__ import annotations

from dataclasses import dataclass

import pytest

from service.runtime_identity import request_harness_identity

VERSION_ID = "harness-1.0.0"
MANIFEST_DIGEST = "a" * 64


@dataclass(frozen=True)
class _Request:
    runtime_identity: tuple[str, str, str] | None


def _config() -> dict[str, object]:
    return {
        "harness_version": {
            "id": VERSION_ID,
            "channel": "stable",
            "manifest_digest": MANIFEST_DIGEST,
        }
    }


def _environment() -> dict[str, str]:
    return {
        "RATSNEST_HARNESS_VERSION_ID": VERSION_ID,
        "RATSNEST_HARNESS_CHANNEL": "stable",
        "RATSNEST_HARNESS_MANIFEST_DIGEST": MANIFEST_DIGEST,
    }


def test_signed_internal_harness_identity_match_is_accepted() -> None:
    identity = request_harness_identity(
        _Request(("principal", "tenant", "project")),
        _config(),
        environ=_environment(),
    )

    assert identity is not None
    assert identity.version_id == VERSION_ID
    assert identity.channel == "stable"
    assert identity.manifest_digest == MANIFEST_DIGEST


@pytest.mark.parametrize(
    ("environment_key", "different_value"),
    [
        ("RATSNEST_HARNESS_VERSION_ID", "harness-2.0.0"),
        ("RATSNEST_HARNESS_CHANNEL", "canary"),
        ("RATSNEST_HARNESS_MANIFEST_DIGEST", "b" * 64),
    ],
)
def test_signed_internal_harness_identity_mismatch_is_rejected(
    environment_key: str,
    different_value: str,
) -> None:
    environment = _environment()
    environment[environment_key] = different_value

    with pytest.raises(ValueError, match="does not match"):
        request_harness_identity(
            _Request(("principal", "tenant", "project")),
            _config(),
            environ=environment,
        )


def test_signed_internal_run_without_harness_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="require harness_version"):
        request_harness_identity(
            _Request(("principal", "tenant", "project")),
            {},
            environ=_environment(),
        )

def test_public_request_cannot_forge_harness_identity() -> None:
    with pytest.raises(ValueError, match="reserved for signed internal"):
        request_harness_identity(
            _Request(None),
            _config(),
            environ=_environment(),
        )
