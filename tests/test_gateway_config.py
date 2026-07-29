from __future__ import annotations

import pytest

from matterhorn.gateway_config import configured_gateway


def _gateway():
    return configured_gateway(
        provider="openai-compatible",
        base_url="http://localhost:11434/v1",
        api_key="test",
        model="test-model",
    )


def test_gateway_timeout_defaults_to_sixty_seconds(monkeypatch) -> None:
    monkeypatch.delenv("MATTERHORN_TIMEOUT", raising=False)

    assert _gateway().timeout == 60.0


def test_gateway_timeout_reads_float_seconds_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("MATTERHORN_TIMEOUT", "12.5")

    assert _gateway().timeout == 12.5


@pytest.mark.parametrize("value", ["not-a-number", "0", "-1", "nan"])
def test_gateway_timeout_rejects_invalid_values(monkeypatch, value) -> None:
    monkeypatch.setenv("MATTERHORN_TIMEOUT", value)

    with pytest.raises(ValueError, match="MATTERHORN_TIMEOUT"):
        _gateway()
