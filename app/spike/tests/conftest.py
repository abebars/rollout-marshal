"""Fixtures for the task 1.2 spike tests. The fakes themselves live in fakes.py."""

from __future__ import annotations

import pytest

from app.spike.tests.fakes import FakeFlagsmith


@pytest.fixture
def fake_flagsmith():
    fake = FakeFlagsmith().start()
    yield fake
    fake.stop()


@pytest.fixture
def local_client_factory(fake_flagsmith):
    """make_client for the spike's run_router and run_judge, pointed at the fake.

    It keeps every client it made, so a test can show that each invocation builds its own.
    """
    from flagsmith import EventProcessorConfig

    from app.spike import handler

    made = []

    def make_client(sdk_key: str):
        client = handler.new_flagsmith_client(
            sdk_key,
            api_url=fake_flagsmith.api_url,
            event_processor_config=EventProcessorConfig(events_api_url=fake_flagsmith.events_url),
        )
        made.append(client)
        return client

    make_client.made = made
    return make_client
