import importlib
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _module():
    sys.modules.pop("discord_integration", None)
    return importlib.import_module("discord_integration")


def test_authorize_fails_closed_without_token(monkeypatch):
    monkeypatch.delenv("MYCELIUM_BOT_TOKEN", raising=False)
    mod = _module()
    with pytest.raises(mod.IntegrationAuthError) as exc:
        mod.authorize("Bearer anything")
    assert exc.value.status_code == 503


def test_authorize_requires_exact_bearer_token(monkeypatch):
    monkeypatch.setenv("MYCELIUM_BOT_TOKEN", "test-secret-token")
    mod = _module()
    mod.authorize("Bearer test-secret-token")
    with pytest.raises(mod.IntegrationAuthError) as exc:
        mod.authorize("Bearer wrong")
    assert exc.value.status_code == 401


def test_events_omit_sensitive_message_fields(monkeypatch):
    mod = _module()
    rows = [{
        "id": 11,
        "event": "added",
        "title": "Example Film",
        "success": 1,
        "created_at": "2026-08-18 12:00:00",
    }]
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = rows
    context = MagicMock()
    context.__enter__.return_value = connection
    fake_db = SimpleNamespace(_connect=lambda: context)
    monkeypatch.setitem(sys.modules, "db", fake_db)

    result = mod.get_events(after_id=10, limit=500)

    assert result["next_cursor"] == 11
    assert result["events"] == [{
        "id": 11,
        "type": "added",
        "title": "Example Film",
        "status": "ok",
        "occurred_at": "2026-08-18 12:00:00",
    }]
    _, params = connection.execute.call_args.args
    assert params == (10, 100)
