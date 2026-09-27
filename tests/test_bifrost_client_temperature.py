"""BifrostClient.consult() must omit `temperature` unless the caller passes one.

Regression coverage for SPEC-consult-temperature-2026-07-29.md: some provider
endpoints (Nebius/Kimi-K3) reject any client-supplied temperature outright, so
the default must OMIT the field rather than peg it to a fixed value.
"""

from __future__ import annotations

from mori_advisor.bifrost_client import BifrostClient


class _FakeCompletions:
    def __init__(self) -> None:
        self.captured_kwargs: dict = {}

    def create(self, **kwargs):
        self.captured_kwargs = kwargs

        class _Message:
            content = "stub response"

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        return _Response()


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()
        self.chat = _FakeChat(self.completions)


def _client_with_fake(monkeypatch) -> tuple[BifrostClient, _FakeCompletions]:
    client = BifrostClient()
    fake = _FakeClient()
    monkeypatch.setattr(client, "_client_for", lambda vk="advisor": (fake, "stub-model"))
    return client, fake.completions


def test_temperature_omitted_by_default(monkeypatch):
    client, completions = _client_with_fake(monkeypatch)

    client.consult(system="sys", user="usr")

    assert "temperature" not in completions.captured_kwargs


def test_explicit_temperature_is_still_sent(monkeypatch):
    client, completions = _client_with_fake(monkeypatch)

    client.consult(system="sys", user="usr", temperature=0.0)

    assert completions.captured_kwargs["temperature"] == 0.0


def test_explicit_zero_is_not_swallowed_as_falsy(monkeypatch):
    # Same case as above, asserted separately: `if temperature:` would drop a
    # real 0.0 (extraction paths depend on determinism), while `if temperature
    # is not None:` keeps it. This is the whole reason for the None check.
    client, completions = _client_with_fake(monkeypatch)

    client.consult(system="sys", user="usr", temperature=0.0)

    assert "temperature" in completions.captured_kwargs
    assert completions.captured_kwargs["temperature"] == 0.0
