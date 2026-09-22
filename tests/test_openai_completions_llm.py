import pytest

pytest.importorskip("httpx")

from voice_engine.core.types import ChatMessage
from voice_engine.providers.openai_completions import llm as oc_llm
from voice_engine.providers.openai_completions.llm import _render_prompt
from voice_engine.providers.registry import create


def test_openai_completions_registered():
    assert create("llm", "openai_completions")


def test_render_prompt_includes_system_and_turns():
    prompt = _render_prompt([ChatMessage("user", "hi")], "Be brief.")
    assert prompt == "System: Be brief.\nUser: hi\nAssistant:"


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines
        self.calls = []

    def stream(self, method, url, json=None, headers=None):
        self.calls.append((method, url, json, headers))
        return _FakeStreamResponse(self._lines)


@pytest.mark.asyncio
async def test_generate_yields_tokens_until_done(monkeypatch):
    lines = [
        'data: {"choices":[{"text":"Hel"}]}',
        'data: {"choices":[{"text":"lo"}]}',
        "data: [DONE]",
    ]
    provider = oc_llm.OpenAICompletionsLLM(url="http://fake/v1/completions")
    fake_client = _FakeClient(lines)
    monkeypatch.setattr(provider, "_get_client", lambda: fake_client)

    tokens = [t.text async for t in provider.generate([ChatMessage("user", "hi")])]

    assert tokens == ["Hel", "lo"]
    assert fake_client.calls[0][1] == "http://fake/v1/completions"
    assert fake_client.calls[0][2]["stream"] is True
