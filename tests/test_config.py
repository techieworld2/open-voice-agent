import os
from pathlib import Path

from voice_engine.config import load_env


def test_env_loads_quotes_and_preserves_existing_environment(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('TEST_VOICE_ONE="hello there"\nTEST_VOICE_TWO=from_file\nTEST_VOICE_THREE=mock # comment\n')
    monkeypatch.delenv("TEST_VOICE_ONE", raising=False)
    monkeypatch.setenv("TEST_VOICE_TWO", "existing")
    monkeypatch.delenv("TEST_VOICE_THREE", raising=False)
    load_env(path)
    assert os.environ["TEST_VOICE_ONE"] == "hello there"
    assert os.environ["TEST_VOICE_TWO"] == "existing"
    assert os.environ["TEST_VOICE_THREE"] == "mock"


def test_example_providers_are_key_free_mock_defaults():
    example = Path(__file__).resolve().parents[1] / ".env.example"
    text = example.read_text()
    for kind in ("STT", "LLM", "TTS"):
        assert f"VOICE_ENGINE_{kind}=mock" in text.splitlines()
