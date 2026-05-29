"""Verify that the trial-expiring email is gated by billing_ui_enabled.

When billing UI is hidden (pre-payment beta default), the user has no way
to act on a trial reminder, so we suppress the send entirely. PR B
2026-05-29 — spec section 1.
"""
from unittest.mock import AsyncMock

import pytest

from app.services import subscription_service


@pytest.mark.asyncio
async def test_send_trial_email_safe_no_op_when_flag_off(monkeypatch):
    """When billing_ui_enabled=False, the trial reminder email is not sent."""
    monkeypatch.setattr(
        "app.services.subscription_service.settings.billing_ui_enabled",
        False,
    )
    sent = AsyncMock()
    monkeypatch.setattr(
        "app.services.subscription_service.send_trial_expiring_email",
        sent,
    )
    await subscription_service._send_trial_email_safe(
        email="u@x.com",
        days_left=3,
        org_name="Doe Household",
    )
    sent.assert_not_called()


@pytest.mark.asyncio
async def test_send_trial_email_safe_sends_when_flag_on(monkeypatch):
    """When billing_ui_enabled=True, the trial reminder email IS sent."""
    monkeypatch.setattr(
        "app.services.subscription_service.settings.billing_ui_enabled",
        True,
    )
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.subscription_service.send_trial_expiring_email",
        sent,
    )
    await subscription_service._send_trial_email_safe(
        email="u@x.com",
        days_left=3,
        org_name="Doe Household",
    )
    sent.assert_called_once_with("u@x.com", 3, "Doe Household")
