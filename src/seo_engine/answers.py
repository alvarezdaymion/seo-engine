"""Recorded answers: the only thing a page can be built from.

A recorded answer is a customer question that a real person on your team answered on video. It
is two files in the answers folder:

  <id>.json   question, speaker, video URL, recording date, target queries, approved sources,
              lines cut from the approved edit, and the approval record
  <id>.txt    the transcript

A recording is approved only when its status and both approval hashes match the current
transcript and publication metadata. Transcript line endings are normalized. Editing either
the transcript or metadata withdraws approval until someone approves it again
(`seo-engine approve <id>`). Older approvals without a revision hash fail closed.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .text import content_words, dice, is_slug, sha256, words


class NoApprovedRecording(Exception):
    """Raised instead of creating a page that has no approved recorded answer behind it."""


@dataclass
class Source:
    url: str
    title: str
    supports: str  # words from the transcript this source backs, for example "10 percent longer"


@dataclass
class RecordedAnswer:
    id: str
    question: str
    speaker_name: str
    video_url: str
    transcript: str
    transcript_sha256: str
    speaker_role: str = ""
    thumbnail_url: str = ""
    recorded_on: str = ""
    approval: dict[str, Any] = field(default_factory=dict)
    cut: list[str] = field(default_factory=list)
    target_queries: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    path: Path | None = None

    @property
    def status(self) -> str:
        return str(self.approval.get("status") or "recorded")

    @property
    def approved(self) -> bool:
        return not approval_problems(self)

    @property
    def revision_sha256(self) -> str:
        data = asdict(self)
        for key in ("approval", "path", "transcript_sha256"):
            data.pop(key)
        return sha256(json.dumps(data, sort_keys=True, ensure_ascii=False))


def approval_problems(answer: RecordedAnswer | None) -> list[str]:
    """Everything that stops this recording from backing a page. Empty means approved."""
    if answer is None:
        return ["no recorded answer"]
    problems = []
    if answer.status != "approved":
        problems.append('not approved: status is "%s"' % answer.status)
    elif answer.approval.get("transcript_sha256") != answer.transcript_sha256:
        problems.append("transcript changed after approval")
    elif answer.approval.get("revision_sha256") != answer.revision_sha256:
        problems.append("recording metadata changed or needs a new approval")
    if not answer.transcript.strip():
        problems.append("transcript is empty")
    if not answer.video_url.startswith("https://"):
        problems.append("no https video URL")
    if not answer.speaker_name.strip():
        problems.append("no speaker name")
    return problems


def require_approved(answer: RecordedAnswer | None) -> RecordedAnswer:
    problems = approval_problems(answer)
    if problems or answer is None:
        raise NoApprovedRecording("%s: %s" % (answer.id if answer else "page", "; ".join(problems)))
    return answer


def load_answer(path: Path) -> RecordedAnswer:
    meta = json.loads(path.read_text())
    answer_id = str(meta.get("id", ""))
    if not is_slug(answer_id) or answer_id != path.stem:
        raise ValueError("%s: id must be lowercase letters, digits and dashes and match the file name" % path.name)
    transcript_path = path.parent / str(meta.get("transcript_file") or answer_id + ".txt")
    raw = transcript_path.read_bytes() if transcript_path.exists() else b""
    transcript = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    speaker = meta.get("speaker") or {}
    if not isinstance(speaker, dict):
        raise ValueError("%s: speaker must be an object with name and role" % path.name)
    links = [str(s.get("url", "")) for s in meta.get("sources", [])] + [str(meta.get("thumbnail_url") or "https://")]
    if not all(u.startswith(("https://", "http://")) for u in links):
        raise ValueError("%s: source and thumbnail URLs must start with https:// or http://" % path.name)
    return RecordedAnswer(
        id=answer_id, question=str(meta.get("question", "")).strip(),
        speaker_name=str(speaker.get("name", "")), speaker_role=str(speaker.get("role", "")),
        video_url=str(meta.get("video_url", "")), thumbnail_url=str(meta.get("thumbnail_url", "")),
        recorded_on=str(meta.get("recorded_on", "")), transcript=transcript,
        transcript_sha256=sha256(transcript), approval=dict(meta.get("approval") or {}),
        cut=[str(c) for c in meta.get("cut", [])], target_queries=[str(q) for q in meta.get("target_queries", [])],
        sources=[Source(str(s["url"]), str(s.get("title", s["url"])), str(s.get("supports", "")))
                 for s in meta.get("sources", [])],
        path=path)


def load_answers(directory: Path) -> tuple[list[RecordedAnswer], list[str]]:
    """All recorded answers in the folder, plus one error line per file that could not be read."""
    answers, errors = [], []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            answer = load_answer(path)
            if not answer.question:
                raise ValueError("%s: question is empty" % path.name)
            answers.append(answer)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            errors.append(str(e)[:200])
    return answers, errors


def approve(path: Path, by: str, today: dt.date) -> RecordedAnswer:
    """Record approval of the transcript and publication metadata exactly as they are now."""
    answer = load_answer(path)
    if not answer.transcript.strip():
        raise ValueError("%s has no transcript to approve" % answer.id)
    meta = json.loads(path.read_text())
    meta["approval"] = {"status": "approved", "by": by, "on": today.isoformat(),
                        "transcript_sha256": answer.transcript_sha256, "revision_sha256": answer.revision_sha256}
    path.write_text(json.dumps(meta, indent=2) + "\n")
    return load_answer(path)


def match_score(query: str, answer: RecordedAnswer) -> float:
    q = " ".join(words(query))
    if any(q == " ".join(words(t)) for t in answer.target_queries):
        return 1.0
    qw = content_words(query)
    return max([dice(qw, content_words(answer.question))] + [dice(qw, content_words(t)) for t in answer.target_queries])


def best_match(query: str, answers: list[RecordedAnswer], threshold: float) -> tuple[RecordedAnswer, float] | None:
    """The recorded answer that best covers a search query, approved or not, if any clears the threshold."""
    scored = [(match_score(query, a), a) for a in answers]
    scored = [s for s in scored if s[0] >= threshold]
    if not scored:
        return None
    scored.sort(key=lambda s: (-s[0], not s[1].approved, s[1].id))
    return scored[0][1], scored[0][0]
