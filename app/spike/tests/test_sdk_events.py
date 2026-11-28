"""What the installed Flagsmith SDK (flagsmith 6.7.0, uv.lock) actually sends, shown locally.

Each test runs the real SDK against FakeFlagsmith on 127.0.0.1 (conftest.py), so these are
facts about the SDK, not about Flagsmith's servers. Task 1.2's live run checks the servers.
"""

from __future__ import annotations

import logging
import threading

import pytest
from flagsmith import EventProcessorConfig, Flagsmith

from app.spike.tests.fakes import DUMMY_SDK_KEY
from contracts import names


def client_for(fake, **config) -> Flagsmith:
    return Flagsmith(
        environment_key=DUMMY_SDK_KEY,
        api_url=fake.api_url,
        **names.FLAGSMITH_CLIENT_OPTIONS,
        event_processor_config=EventProcessorConfig(events_api_url=fake.events_url, **config),
    )


def test_a_numeric_value_travels_as_a_json_string(fake_flagsmith):
    client = client_for(fake_flagsmith)
    for value in (1, 0, 0.8, 1234, True, None):
        client.track_event("spike_event", identifier=names.identity(1), value=value)
    client.flush_events()

    sent = [event["value"] for event in fake_flagsmith.events()]
    # analytics.py: "value": str(value) if value is not None else None. A bool becomes "True",
    # which is why design.md sends answer_helpful as the integer 1 or 0.
    assert sent == ["1", "0", "0.8", "1234", "True", None]


def test_the_event_payload_and_headers(fake_flagsmith):
    client = client_for(fake_flagsmith)
    client.track_event("spike_event", identifier=names.identity(7), value=1)
    client.flush_events()

    [event] = fake_flagsmith.events()
    assert event["event"] == "spike_event"
    assert event["feature_name"] is None
    assert event["identifier"] == "user-007"
    assert event["traits"] is None
    assert event["metadata"] == {"sdk_version": "6.7.0"}
    assert isinstance(event["timestamp"], int)  # milliseconds since the epoch, set at track time
    headers = {key.lower(): value for key, value in fake_flagsmith.event_headers[0].items()}
    assert headers["x-environment-key"] == DUMMY_SDK_KEY
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert headers["flagsmith-sdk-user-agent"] == "flagsmith-python-sdk/6.7.0"


def test_flush_events_returns_only_after_every_buffered_event_is_delivered(fake_flagsmith):
    client = client_for(fake_flagsmith)  # the defaults: 1,000 buffered events, 10 s timer
    for n in range(1, names.IDENTITY_COUNT + 1):
        client.track_event("spike_event", identifier=names.identity(n), value=n % 2)

    client.flush_events()

    # No sleep: when flush_events() returns, the fake already holds all 150, in one batch.
    assert len(fake_flagsmith.events()) == names.IDENTITY_COUNT
    assert len(fake_flagsmith.event_batches) == 1


def test_max_buffer_items_one_drops_events_tracked_in_a_loop(fake_flagsmith, caplog):
    """The setting Flagsmith's docs suggest for short scripts loses events in a tight loop.

    With max_buffer_items=1 the buffer holds one event. While the worker thread is sending,
    every new event pushes the previous one out (analytics.py, _drop_oldest_events).
    """
    fake_flagsmith.events_gate = threading.Event()  # hold the first send open
    client = client_for(fake_flagsmith, max_buffer_items=1)
    caplog.set_level(logging.WARNING, logger="flagsmith")

    for n in range(1, names.IDENTITY_COUNT + 1):
        client.track_event("spike_event", identifier=names.identity(n), value=1)
    fake_flagsmith.events_gate.set()
    client.flush_events()

    delivered = len(fake_flagsmith.events())
    assert delivered <= 2  # the one in flight and the newest; the rest were dropped
    assert "Event buffer was full: dropped the" in caplog.text


def test_three_events_in_a_row_also_lose_events_with_max_buffer_items_one(fake_flagsmith):
    """rm-judge sends three events per answer; this setting would drop some of them."""
    fake_flagsmith.events_gate = threading.Event()
    client = client_for(fake_flagsmith, max_buffer_items=1)
    for event in names.EVENTS:
        client.track_event(event, identifier=names.identity(1), value=1)
    fake_flagsmith.events_gate.set()
    client.flush_events()

    assert len(fake_flagsmith.events()) < len(names.EVENTS)


def test_identical_exposures_in_one_send_are_sent_once(fake_flagsmith):
    client = client_for(fake_flagsmith)
    for _ in range(3):
        client.track_exposure_event(
            "spike_flag", identifier=names.identity(1), value="cheaper", metadata={"x": 1}
        )
    client.flush_events()

    [exposure] = fake_flagsmith.events()
    assert exposure["event"] == "$flag_exposure"
    assert exposure["feature_name"] == "spike_flag"
    assert exposure["value"] == "cheaper"
    assert exposure["metadata"] == {"x": 1, "sdk_version": "6.7.0"}


def test_an_exposure_without_an_identifier_is_not_sent(fake_flagsmith, caplog):
    client = client_for(fake_flagsmith)
    caplog.set_level(logging.WARNING, logger="flagsmith")
    client.track_exposure_event("spike_flag", identifier=None, value="cheaper")
    client.flush_events()

    assert fake_flagsmith.events() == []
    assert "an exposure requires an identifier" in caplog.text


def test_track_event_needs_events_enabled(fake_flagsmith):
    client = Flagsmith(environment_key=DUMMY_SDK_KEY, api_url=fake_flagsmith.api_url)
    with pytest.raises(ValueError, match="Events must be enabled"):
        client.track_event("spike_event", identifier=names.identity(1), value=1)


def test_a_refused_key_stops_event_tracking(fake_flagsmith, caplog):
    fake_flagsmith.events_status = 401
    client = client_for(fake_flagsmith)
    caplog.set_level(logging.ERROR, logger="flagsmith")
    client.track_event("spike_event", identifier=names.identity(1), value=1)
    client.flush_events()
    client.track_event("spike_event", identifier=names.identity(2), value=1)
    client.flush_events()

    assert len(fake_flagsmith.event_batches) == 1  # nothing is sent after the refusal
    assert "Events API refused the environment key with 401" in caplog.text
