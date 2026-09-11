"""Shared pytest configuration.

Keeps the configuration layer hermetic: without the autouse fixture below a
test that sets ``--config``/``PONTE_CONFIG`` would leak that state into the
next test, and ``get_config()`` could pick up the developer's real
``~/.config/ponte/config.toml``.
"""

from __future__ import annotations

import pytest

from ponte import config as config_module


def pytest_report_header() -> str:
    """Show where the config layer resolves to, which explains most failures."""
    return "ponte config search path: " + " | ".join(config_module.config_search_paths())


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch):
    """Reset the config override, the environment override and the cache."""
    monkeypatch.delenv(config_module.CONFIG_ENV_VAR, raising=False)
    config_module.set_config_path(None)
    config_module.clear_config_cache()
    yield
    config_module.set_config_path(None)
    config_module.clear_config_cache()
