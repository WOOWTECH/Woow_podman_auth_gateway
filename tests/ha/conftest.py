"""Fixtures for the woow_protected_apps tests, run in a real HA core (see requirements.txt).

These tests need Linux and the pinned pytest-homeassistant-custom-component, so they live
outside unittest discovery (tests/ha has no __init__.py) and run in their own CI job:

    pip install -r tests/ha/requirements.txt
    pytest tests/ha
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPONENTS = str(REPO / "ha" / "custom_components")

# The key both ends share. Test-only: 32 bytes, hex.
TEST_SECRET = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load the component from ha/custom_components."""
    # HA's loader looks in the custom_components package the plugin imports from its own
    # testing_config; add ours to it.
    import custom_components

    if COMPONENTS not in custom_components.__path__:
        custom_components.__path__.append(COMPONENTS)
    yield


@pytest.fixture
def secret_file(hass, tmp_path):
    """A config dir holding the ticket secret, as restore.sh leaves it."""
    hass.config.config_dir = str(tmp_path)
    path = tmp_path / ".woow_ticket_secret"
    path.write_text(TEST_SECRET + "\n", encoding="utf-8")
    return path
