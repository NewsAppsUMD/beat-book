import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(autouse=True)
def _default_provider_settings(monkeypatch):
    """Start every test from the default providers, whatever the local .env
    says (importing app loads it). Tests that need Ollama set it themselves."""
    for name in ("RESEARCH_PROVIDER", "RESEARCH_OLLAMA_MODEL", "OLLAMA_THINK"):
        monkeypatch.delenv(name, raising=False)
