"""Test isolation: never read or write the machine's real configuration.

Two tests here asserted that credentials were absent, and passed for months —
on machines where they were. The moment a real RetroAchievements key was saved
in Settings, they began failing, because `credentials_from_config` falls back to
the user's own `credentials.json` and the suite was reading it.

That is worse than a flaky test. A suite that reads the developer's home
directory can also *write* to it, and a test that clears credentials would have
deleted a real key.

So every test runs against throwaway directories, redirected here rather than in
each test that happens to remember.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_config(tmp_path_factory):
    """Point preferences and credentials at a temporary directory for the run."""
    from rose_gamelab.ui import preferences

    folder = tmp_path_factory.mktemp("config")

    original_credentials = preferences.CREDENTIALS_PATH
    original_preferences = preferences.DEFAULT_PATH

    preferences.CREDENTIALS_PATH = folder / "credentials.json"
    preferences.DEFAULT_PATH = folder / "preferences.json"

    yield

    preferences.CREDENTIALS_PATH = original_credentials
    preferences.DEFAULT_PATH = original_preferences


@pytest.fixture(autouse=True)
def _no_real_environment(monkeypatch):
    """Clear credential environment variables.

    They are read before any file, so a developer with `RA_API_KEY` exported
    would see different results from CI — and from another developer.
    """
    for name in ("RA_USERNAME", "RA_API_KEY", "STEAMGRIDDB_API_KEY"):
        monkeypatch.delenv(name, raising=False)
