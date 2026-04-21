"""Shared pytest fixtures for OESDevTeam tests.

Placed here (not in individual test files) so that every test automatically
gets a clean environment. This avoids the debugging nightmare of "test A
passes alone but fails after test B" caused by leaked module-level state.
"""

from __future__ import annotations

import os

import pytest

from src.config import reload_config


@pytest.fixture(autouse=True)
def _isolate_oesdevteam_env(monkeypatch):
    """Strip every ``OESDEVTEAM_*`` variable and reset the YAML cache.

    Why autouse + monkeypatch: tests that mutate ``os.environ`` directly
    leak into subsequent tests. monkeypatch reverts on teardown, and
    autouse ensures *every* test gets the fresh slate, not only those
    that remembered to ask for the fixture.
    """
    for key in list(os.environ):
        if key.startswith("OESDEVTEAM_"):
            monkeypatch.delenv(key, raising=False)

    # YAML cache is module-global; clear it so each test sees a fresh load
    # when it (or the code under test) calls into the config loader.
    reload_config()
    yield
    reload_config()
