"""Shared test imports; Telegram checks also run independently of the root suite."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))


@pytest.fixture
def policy():
    return json.loads((ROOT / "infra/hermes/telegram-settings.json").read_text())


@pytest.fixture
def runtime():
    spec = importlib.util.spec_from_file_location(
        "telegram_runtime", ROOT / "infra/hermes/telegram-runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
