"""Startup must not require keeping the initial administrator password in .env."""
from unittest.mock import Mock

import pytest

from app import bootstrap


def test_headless_restart_without_bootstrap_credentials(monkeypatch):
    monkeypatch.setattr(bootstrap.settings, 'bootstrap_username', '')
    monkeypatch.setattr(bootstrap.settings, 'bootstrap_password', '')
    monkeypatch.setattr(bootstrap.sys.stdin, 'isatty', lambda: False)
    initialize_existing_installation = Mock()
    monkeypatch.setattr(bootstrap, 'bootstrap', initialize_existing_installation)

    bootstrap.main()

    initialize_existing_installation.assert_called_once_with()


def test_headless_startup_preserves_missing_administrator_failure(monkeypatch):
    monkeypatch.setattr(bootstrap.settings, 'bootstrap_username', '')
    monkeypatch.setattr(bootstrap.settings, 'bootstrap_password', '')
    monkeypatch.setattr(bootstrap.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(bootstrap, 'bootstrap', Mock(side_effect=RuntimeError('No superadmin exists')))

    with pytest.raises(RuntimeError, match='No superadmin exists'):
        bootstrap.main()


def test_explicit_credentials_do_not_prompt_on_terminal(monkeypatch):
    monkeypatch.setattr(bootstrap.settings, 'bootstrap_username', 'test_admin')
    monkeypatch.setattr(bootstrap.settings, 'bootstrap_password', 'test-password-123!')
    monkeypatch.setattr(bootstrap.sys.stdin, 'isatty', lambda: True)
    initialize_new_installation = Mock()
    monkeypatch.setattr(bootstrap, 'bootstrap', initialize_new_installation)

    bootstrap.main()

    initialize_new_installation.assert_called_once_with()
