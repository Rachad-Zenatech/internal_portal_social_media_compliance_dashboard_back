"""Shared test setup.

The auth module refuses to load without a signing secret of at least 32 characters, and several test modules import it
while pytest is still collecting. CI has no secrets, so a throwaway one is set here, before any test module is imported.
It is only ever used by the tests; a real value in the environment is left alone.
"""

import os

os.environ.setdefault("JWT_SECRET", "test-only-secret-not-used-anywhere-else-0123456789")
