from voice_engine.providers.registry import create


def test_mock_registry():
    for kind in ("stt", "llm", "tts"):
        __import__(f"voice_engine.providers.mock.{kind}")
        assert create(kind, "mock")
