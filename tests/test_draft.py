import json
from types import SimpleNamespace

from seo_engine import config
from seo_engine.draft import grounding_problems, make_draft, template_draft

TRANSCRIPT = [
    "Good question.",
    "A dripping kitchen faucet usually means the cartridge has worn out.",
    "Hard water wears those parts faster than soft water does.",
    "Do not overtighten the handle, because that only crushes the washer.",
]
MODEL = {**config.DEFAULTS["draft"], "mode": "model"}


class FakeClient:
    """Stands in for anthropic.Anthropic(): records the request, returns a canned answer."""

    def __init__(self, answer=None, stop_reason="end_turn", raw=None):
        text = raw if raw is not None else json.dumps({"answer": answer or []})
        self.response = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def test_template_skips_short_openers():
    d = template_draft(TRANSCRIPT, 2)
    assert d.method == "template" and d.sentences == TRANSCRIPT[1:3]


def test_template_mode_never_calls_a_model():
    client = FakeClient()
    draft = make_draft("Why is my faucet dripping?", TRANSCRIPT, config.DEFAULTS["draft"], 2, client)
    assert draft.method == "template"
    assert client.calls == []


def test_grounded_model_draft_is_used_and_may_tidy():
    client = FakeClient([{"text": "A dripping kitchen faucet usually means the cartridge has worn out.", "from": [2]},
                         {"text": "Hard water wears those parts faster.", "from": [3]}])
    d = make_draft("Why is my faucet dripping?", TRANSCRIPT, MODEL, 2, client)
    assert d.method == "model" and d.sentences[1] == "Hard water wears those parts faster."
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "2. A dripping kitchen faucet" in call["messages"][0]["content"]


def test_a_model_draft_that_adds_a_word_is_rejected_for_the_template():
    client = FakeClient([{"text": "A dripping faucet always means the cartridge failed, guaranteed.", "from": [2]}])
    d = make_draft("Why is my faucet dripping?", TRANSCRIPT, MODEL, 2, client)
    assert d.method == "template" and "adds words" in d.note and "guaranteed" in d.note
    assert d.sentences == TRANSCRIPT[1:3]


def test_a_model_draft_that_adds_a_number_is_rejected():
    assert grounding_problems("Hard water wears those parts 3 times faster.", [TRANSCRIPT[2]])


def test_a_model_draft_that_drops_a_negation_is_rejected():
    problems = grounding_problems("Overtighten the handle.", [TRANSCRIPT[3]])
    assert "drops a negation" in problems


def test_model_output_with_bad_numbers_or_shape_falls_back():
    for client in (FakeClient([{"text": "Good question.", "from": [9]}]), FakeClient(raw="not json"),
                   FakeClient([]), FakeClient([{"text": "x", "from": []}])):
        d = make_draft("Why is my faucet dripping?", TRANSCRIPT, MODEL, 2, client)
        assert d.method == "template" and d.note.startswith("model draft rejected")


def test_a_refusal_falls_back_to_the_template():
    d = make_draft("q", TRANSCRIPT, MODEL, 2, FakeClient(stop_reason="refusal"))
    assert d.method == "template" and "declined" in d.note


def test_fallbacks_can_be_turned_off():
    client = FakeClient([{"text": TRANSCRIPT[1], "from": [2]}])
    make_draft("q", TRANSCRIPT, {**MODEL, "fallbacks": False}, 2, client)
    assert "fallbacks" not in client.calls[0] and "betas" not in client.calls[0]
