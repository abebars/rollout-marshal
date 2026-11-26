"""Shared fixtures for the rm-invoker tests."""

from __future__ import annotations

import os

import pytest

_AWS_ENV = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_MAX_ATTEMPTS",
    "AWS_RETRY_MODE",
)


@pytest.fixture(autouse=True)
def no_aws_credentials(monkeypatch):
    """Every test runs with no credentials, no AWS config files and no instance metadata,
    so a request that escaped a fake could not be signed, let alone reach AWS."""
    for name in _AWS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
