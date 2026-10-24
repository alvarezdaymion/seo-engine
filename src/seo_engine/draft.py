"""Pick the direct-answer sentences for a page: a deterministic template, or an optional model.

Template (default): the first `answer_sentences` sentences of the approved transcript that have
five words or more, which skips openers like "Good question."

Model ([draft] mode = "model", `uv sync --extra llm`): the model gets the question and the
transcript as numbered sentences and selects whole sentences in their original order. The only
cleanup is deterministic removal of filler sounds and normalization of quotes, capitalization
and whitespace. A changed sentence rejects the whole draft and the template is used instead.
The claim check runs on the result either way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .text import claim_words, has_negation, tidy, words

SYSTEM = (
    "You pick the direct answer for a web page built from a recorded answer. The user message has the "
    "question and the transcript as numbered sentences. Choose the two to four sentences that answer the "
    "question most directly, in reading order. Copy whole sentences exactly; you may remove only "
    "filler sounds such as um and uh. Do not shorten, join or rearrange sentences. "
    "For each sentence, list its one source sentence number."
)

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "array", "items": {
        "type": "object",
        "properties": {"text": {"type": "string"}, "from": {"type": "array", "items": {"type": "integer"}}},
        "required": ["text", "from"], "additionalProperties": False}}},
    "required": ["answer"],
    "additionalProperties": False,
}


class DraftRejected(Exception):
    pass


@dataclass
class Draft:
    sentences: list[str]
    method: str
    note: str = ""


def template_draft(transcript: list[str], n: int) -> Draft:
    """The first n sentences that say something: five words or more, and not a question back."""
    picked = [s for s in transcript if len(words(s)) >= 5 and not s.endswith("?")][:max(1, n)]
    return Draft(picked or transcript[:1], "template")


def grounding_problems(text: str, cited: list[str]) -> list[str]:
    source = " ".join(cited)
    problems = []
    extra = sorted(claim_words(text) - claim_words(source))
    if extra:
        problems.append("adds words not in the cited sentences: " + ", ".join(extra[:8]))
    if has_negation(source) and not has_negation(text):
        problems.append("drops a negation")
    if len(cited) != 1 or tidy(text) != tidy(cited[0]):
        problems.append("must select a whole approved sentence with only filler cleanup")
    return problems


def check_model_output(data: Any, transcript: list[str], max_sentences: int) -> list[str]:
    items = data.get("answer") if isinstance(data, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= max_sentences:
        raise DraftRejected("expected 1 to %d answer sentences" % max_sentences)
    out = []
    last_ref = 0
    for item in items:
        text = str(item.get("text", "")).strip() if isinstance(item, dict) else ""
        refs = item.get("from") if isinstance(item, dict) else None
        if not text or not isinstance(refs, list) or not refs:
            raise DraftRejected("each sentence needs text and the numbers it came from")
        if not all(type(n) is int and 1 <= n <= len(transcript) for n in refs):
            raise DraftRejected("cites a sentence number that does not exist: %s" % refs)
        problems = grounding_problems(text, [transcript[n - 1] for n in refs])
        if problems:
            raise DraftRejected("%r %s" % (text[:60], "; ".join(problems)))
        if refs[0] <= last_ref:
            raise DraftRejected("sentences must be selected in transcript order")
        last_ref = refs[0]
        out.append(tidy(text))
    return out


def model_draft(question: str, transcript: list[str], dcfg: dict[str, Any], client: Any = None,
                max_sentences: int = 4) -> Draft:
    if client is None:
        try:
            import anthropic
        except ImportError as e:
            raise DraftRejected("install the llm extra to use the model draft: uv sync --extra llm") from e
        client = anthropic.Anthropic(max_retries=0)
    numbered = "\n".join("%d. %s" % (i, s) for i, s in enumerate(transcript, 1))
    kwargs: dict[str, Any] = {
        "model": dcfg["model"], "max_tokens": int(dcfg["max_tokens"]), "system": SYSTEM,
        "output_config": {"effort": dcfg["effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": "Question: %s\n\nTranscript:\n%s" % (question, numbered)}],
    }
    if dcfg.get("fallbacks"):
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    else:
        resp = client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        raise DraftRejected("the model declined this request")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise DraftRejected("the model did not return JSON") from e
    return Draft(check_model_output(data, transcript, max_sentences), "model")


def make_draft(question: str, transcript: list[str], dcfg: dict[str, Any], n: int, client: Any = None) -> Draft:
    if dcfg["mode"] != "model":
        return template_draft(transcript, n)
    try:
        return model_draft(question, transcript, dcfg, client)
    except Exception as e:  # any model failure falls back to the template, and says why
        fallback = template_draft(transcript, n)
        reason = str(e)[:200] if isinstance(e, DraftRejected) else type(e).__name__
        fallback.note = "model draft rejected: %s" % reason
        return fallback
