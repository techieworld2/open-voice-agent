from voice_engine.providers.mock import stt, llm, tts
from voice_engine.providers.registry import create


def test_mock_registry():
    assert create("stt","mock")
    assert create("llm","mock")
    assert create("tts","mock")
