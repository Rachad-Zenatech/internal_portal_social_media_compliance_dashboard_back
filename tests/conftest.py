"""Shared test setup.

The auth module refuses to load without a signing secret of at least 32 characters, and several test modules import it
while pytest is still collecting. CI has no secrets, so a throwaway one is set here, before any test module is imported.
It is only ever used by the tests; a real value in the environment is left alone.
"""

import os

os.environ.setdefault("JWT_SECRET", "test-only-secret-not-used-anywhere-else-0123456789")


import pytest


@pytest.fixture(autouse=True)
def _tests_never_reach_the_real_vector_database(monkeypatch):
    """Importing the app loads the developer's .env, which would point every test that switches the vector store 'on'
    (by patching is_enabled) at the real Atlas cluster. Blank the connection string for every test: tests that need a
    collection give themselves a fake one."""
    monkeypatch.setenv("MONGODB_URI", "")
    monkeypatch.setenv("MOOD_AUTO_TRAIN", "off")  # a test that wants automatic training turns it on itself
