"""User-zone validation, account isolation and compatibility with older clients."""
import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import pytest
from pydantic import ValidationError

from models.notifications.review_email_model import EmailTimezonePreference
from services.notifications.review_email import preferences


@pytest.mark.parametrize("zone", ["America/Toronto", "Asia/Manila", "America/Los_Angeles", "UTC"])
def test_valid_iana_zones(zone):
    assert EmailTimezonePreference(time_zone=zone, source="MANUAL").time_zone == zone


@pytest.mark.parametrize("payload", [
    {"time_zone": "EST+garbage"},
    {"time_zone": "America/Toronto", "source": "OTHER"},
    {"time_zone": "../America/Toronto"},
])
def test_invalid_preferences_rejected(payload):
    with pytest.raises(ValidationError):
        EmailTimezonePreference(**payload)


def test_older_clients_default_to_browser_detection():
    assert EmailTimezonePreference(time_zone="Asia/Manila").source == "BROWSER"


def test_saved_preference_identifies_only_the_authenticated_user():
    user_id = str(uuid4())
    stored = {"time_zone": "Asia/Manila", "source": "MANUAL"}
    fetch = AsyncMock(return_value=stored)
    with patch.object(preferences, "fetch_one", fetch):
        assert asyncio.run(preferences.get_timezone(user_id)) == stored
    assert str(fetch.call_args.args[1]) == user_id


def test_upgrade_existing_preferences_table_before_retry():
    stored = {"time_zone": "America/Toronto", "source": "MANUAL"}
    fetch = AsyncMock(side_effect=[asyncpg.UndefinedColumnError(), stored])
    setup = AsyncMock()
    with patch.object(preferences, "fetch_one", fetch), patch.object(preferences, "setup_review_email_schema", setup):
        result = asyncio.run(preferences.save_timezone(str(uuid4()), EmailTimezonePreference(**stored)))
    assert result == stored
    setup.assert_awaited_once()
    assert fetch.call_args.args[-1] == "MANUAL"
