"""The gates every change passes before it is merged or published.

Content gates are checked again right before a merge or a publish, not only at build time, so a
recording whose approval was withdrawn or whose transcript changed in the meantime stops the
release. The GitHub gates (open, head, files, checks, mergeable) live in review.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .answers import RecordedAnswer, approval_problems
from .page import Page


@dataclass
class Gate:
    name: str
    ok: bool
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def recording_gate(page: Page, answers: list[RecordedAnswer]) -> Gate:
    answer = next((a for a in answers if a.id == page.recording_id), None)
    problems = approval_problems(answer)
    if not problems and answer and answer.transcript_sha256 != page.transcript_sha256:
        problems = ["the page was built from a different transcript"]
    return Gate("recording", not problems, "; ".join(problems) or "%s approved" % page.recording_id)


def content_gates(page: Page, answers: list[RecordedAnswer], findings: list[Any]) -> list[Gate]:
    return [recording_gate(page, answers),
            Gate("claims", not findings, "%d finding(s)" % len(findings) if findings else "passed")]


def failed(gates: list[Gate]) -> list[Gate]:
    return [g for g in gates if not g.ok]
