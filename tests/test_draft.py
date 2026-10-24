import json
import sys
from types import SimpleNamespace

import pytest

from seo_engine import config
from seo_engine.draft import DraftRejected, grounding_problems, make_draft, model_draft, template_draft

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
                         {"text": TRANSCRIPT[2], "from": [3]}])
    d = make_draft("Why is my faucet dripping?", TRANSCRIPT, MODEL, 2, client)
    assert d.method == "model" and d.sentences[1] == TRANSCRIPT[2]
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "2. A dripping kitchen faucet" in call["messages"][0]["content"]


def test_a_model_draft_that_adds_a_word_is_rejected_for_the_template():
    client = FakeClient([{"text": "A dripping faucet always means the cartridge failed, guaranteed.", "from": [2]}])
    d = make_draft("Why is my faucet dripping?", TRANSCRIPT, MODEL, 2, client)
    assert d.method == "template" and "adds words" in d.note
    assert "guaranteed" not in d.note
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


def test_sdk_retries_are_disabled_for_metered_requests(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-model-key")
    options = []
    client = FakeClient([{"text": TRANSCRIPT[1], "from": [2]}])

    def factory(**kwargs):
        options.append(kwargs)
        return client

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=factory))
    assert make_draft("question", TRANSCRIPT, MODEL, 2).method == "model"
    assert options == [{"api_key": "synthetic-model-key", "max_retries": 0}]


def test_unexpected_model_errors_do_not_expose_credentials():
    client = FakeClient()

    def fail(**kwargs):
        raise RuntimeError("synthetic-secret-token")

    client.beta.messages.create = fail
    result = make_draft("question", TRANSCRIPT, MODEL, 2, client)
    assert result.method == "template" and "synthetic-secret-token" not in result.note


def test_model_credentials_are_validated_before_client_creation(monkeypatch):
    secret = "synthetic-credential-marker"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret + "\n")
    calls = []
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda **kw: calls.append(kw)))
    with pytest.raises(DraftRejected) as caught:
        model_draft("question", TRANSCRIPT, MODEL)
    assert calls == [] and secret not in str(caught.value)


@pytest.mark.parametrize("during_creation", [False, True])
def test_model_request_boundary_sanitizes_every_exception(monkeypatch, during_creation):
    secret = "synthetic-credential-marker"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    def fail(**kwargs):
        raise DraftRejected(secret)

    if during_creation:
        monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=fail))
        client = None
    else:
        client = FakeClient()
        client.beta.messages.create = fail
    with pytest.raises(DraftRejected) as caught:
        model_draft("question", TRANSCRIPT, MODEL, client)
    assert secret not in str(caught.value)


@pytest.mark.parametrize("item", [
    {"text": "synthetic-credential-marker", "from": [2]},
    {"text": TRANSCRIPT[1], "from": ["synthetic-credential-marker"]},
])
def test_rejected_model_output_cannot_enter_the_saved_draft_note(item):
    result = make_draft("question", TRANSCRIPT, MODEL, 2, FakeClient([item]))
    assert result.method == "template" and "synthetic-credential-marker" not in result.note
