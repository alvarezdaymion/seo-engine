"""Pick one action per run from the action ladder. Never a page without an approved recording.

  1. finish   a pull request from an earlier run: merge it once every gate passes, publish it
              once it is merged
  2. repair   a published page that failed its live check: republish the approved build
  3. build    a page from an approved recorded answer: first the answers that cover the
              highest-ranked "no page" queries, then approved answers with no page yet
  4. snippet  a low-CTR page the engine built: a new meta description from the transcript
  5. idle     nothing to do. An idle run is normal. There is no page quota to fill.

A "no page" query with no approved recording behind it is refused and reported as a question to
record. Pages the engine did not build are reported, never edited.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from .answers import RecordedAnswer, approval_problems, best_match
from .opportunities import KIND_LABELS, Opportunity, norm_url
from .text import slugify

SNIPPET_COOLDOWN_DAYS = 28


@dataclass
class Line:
    kind: str
    target: str
    verdict: str  # build, refused, covered, held, skip, watch, snippet
    detail: str
    impressions: float = 0.0
    position: float = 0.0
    missed_clicks: float = 0.0
    chosen: bool = False

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Action:
    kind: str  # finish, repair, build, snippet
    slug: str
    reason: str
    answer: RecordedAnswer | None = None
    opportunity: Opportunity | None = None


@dataclass
class Plan:
    action: Action | None
    lines: list[Line] = field(default_factory=list)
    note: str = ""

    @property
    def refused(self) -> list[Line]:
        return [ln for ln in self.lines if ln.verdict == "refused"]


def page_slug(answer: RecordedAnswer, records: dict[str, dict[str, Any]]) -> str:
    """The page's URL slug, from the question. Never reuses a slug that belongs to another recording."""
    slug = slugify(answer.question) or answer.id
    rec = records.get(slug)
    if rec and rec.get("recording_id") not in (None, answer.id):
        slug = "%s-%s" % (slug, answer.id)
    return slug


def held_key(answer: RecordedAnswer, rules_sha: str) -> str:
    """A held build is retried only when the transcript, its approval or the rule pack changes."""
    return "%s:%s:%s" % (answer.revision_sha256, answer.approval.get("on", ""), rules_sha)


def plan(opportunities: list[Opportunity], answers: list[RecordedAnswer], records: dict[str, dict[str, Any]],
         failing: list[str], *, threshold: float, rules_sha: str, new_pages_left: int, today: dt.date) -> Plan:
    by_recording = {r.get("recording_id"): r for r in records.values()}
    lines: list[Line] = []
    builds: list[tuple[RecordedAnswer, Opportunity | None, Line]] = []
    snippets: list[tuple[Opportunity, Line]] = []
    claimed: set[str] = set()

    def build_verdict(answer: RecordedAnswer) -> tuple[str, str]:
        problems = approval_problems(answer)
        rec = by_recording.get(answer.id)
        if problems:
            return "refused", "recording %s is %s" % (answer.id, problems[0])
        if answer.id in claimed:
            return "covered", "same recording as a line above (%s)" % answer.id
        if rec and (rec.get("live") or rec.get("status") in ("opening_review", "in_review", "merged")):
            return "covered", "recording %s already has a page (%s)" % (answer.id, rec.get("status"))
        if rec and rec.get("status") in ("held", "closed") and rec.get("held_key") == held_key(answer, rules_sha):
            return "held", "recording %s: %s" % (answer.id, rec.get("reason", "held"))
        return "build", "from recording %s (%s)" % (answer.id, answer.speaker_name)

    for opp in opportunities:
        line = Line(KIND_LABELS[opp.kind], opp.target, "", "", opp.impressions, opp.position, opp.missed_clicks)
        if opp.kind == "no_page":
            match = best_match(opp.query, answers, threshold)
            if match is None:
                line.verdict, line.detail = "refused", "no recorded answer covers it"
            else:
                line.verdict, line.detail = build_verdict(match[0])
                if line.verdict == "build":
                    claimed.add(match[0].id)
                    builds.append((match[0], opp, line))
        elif not opp.managed:
            line.verdict, line.detail = "skip", "not built by seo-engine, so reported and never edited"
        elif opp.kind == "low_ctr":
            rec = next((r for r in records.values() if norm_url(r.get("url", "")) == norm_url(opp.page)), {})
            last = rec.get("snippet_on")
            if last and (today - dt.date.fromisoformat(last)).days < SNIPPET_COOLDOWN_DAYS:
                line.verdict, line.detail = "watch", "description changed on %s; waiting for data" % last
            else:
                line.verdict, line.detail = "snippet", "new meta description from the transcript"
                snippets.append((opp, line))
        else:
            line.verdict = "watch"
            line.detail = "checked live every run; re-record a fuller answer if it keeps slipping"
        lines.append(line)

    for answer in sorted(answers, key=lambda a: (str(a.approval.get("on", "")), a.id)):
        if not answer.approved or answer.id in claimed:
            continue
        verdict, detail = build_verdict(answer)
        if verdict == "covered":
            continue
        line = Line("recording", '"%s"' % answer.question, verdict, detail if verdict != "build"
                    else "approved recording %s, no search data for it yet" % answer.id)
        lines.append(line)
        if verdict == "build":
            claimed.add(answer.id)
            builds.append((answer, None, line))

    pending = next((r for r in records.values() if r.get("status") in ("opening_review", "in_review", "merged")), None)
    action, note = None, ""
    if pending:
        action = Action("finish", pending["slug"], "%s change from an earlier run" % pending["status"])
    elif failing:
        action = Action("repair", failing[0], "the published page failed its live check")
    elif builds and new_pages_left <= 0:
        note = "daily new-page cap reached; the next run builds %s" % builds[0][0].id
    elif builds:
        answer, opp, line = builds[0]
        line.chosen = True
        action = Action("build", page_slug(answer, records), line.detail, answer, opp)
    if action is None and snippets:
        opp, line = snippets[0]
        line.chosen = True
        slug = next((s for s, r in records.items() if norm_url(r.get("url", "")) == norm_url(opp.page)), "")
        action = Action("snippet", slug, "low CTR at position %.1f" % opp.position, opportunity=opp)
    if action is None and not note:
        note = "nothing to do: no approved recording is waiting and every page passes"
    return Plan(action, lines, note)
