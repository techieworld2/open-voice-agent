import pytest

from voice_engine.core.audio import AudioProfile
from voice_engine.core.turn_detector import BackchannelGuard, TRAILING_WORDS, TurnDetector


@pytest.mark.parametrize("mode,base,trailing", [("wideband", 300, 950), ("telephony", 350, 1100)])
@pytest.mark.parametrize("word", sorted(TRAILING_WORDS))
def test_semantic_trailing_words(mode, base, trailing, word):
    detector = TurnDetector(AudioProfile.select(mode))
    detector.update(f"I would like to go {word}.")
    assert detector.silence_ms == trailing
    assert detector.reason == "Trailing Clause"
    detector.update("I would like to go home.")
    assert detector.silence_ms == base
    assert detector.reason == "Complete"


@pytest.mark.parametrize("text", ["Are you there?", "Thanks!", "That's all", ""])
def test_complete_clauses(text):
    detector = TurnDetector(AudioProfile.select())
    detector.update(text)
    assert detector.silence_ms == 300


@pytest.mark.parametrize("text", ["Yeah!", "uh-huh", "uh huh", "yep", "okay", "right", "mhm"])
def test_passive_backchannels_never_cancel(text):
    assert not BackchannelGuard().confirm(600, text, final=True)


def test_noise_duration_and_genuine_interruption():
    guard = BackchannelGuard()
    assert not guard.confirm(300, "stop please", final=True)
    assert not guard.confirm(1000, "", final=False)
    assert guard.confirm(320, "stop please")
    assert guard.confirm(400, "yeah please stop", final=True)
