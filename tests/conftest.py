"""Shared fixtures for the Home Assistant integration tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# pytest は tests/ しか sys.path に載せないため、Home Assistant が
# custom_components/ を import で解決できるようリポジトリルートを通す。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ for every test."""
    yield
