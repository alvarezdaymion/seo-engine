"""Check every claim on a page against a rule pack, and apply the only fixes that are safe.

A rule pack is a TOML file (start from `templates/rules.example.toml`):

  [[banned]]        phrases that may not appear anywhere on the page
  [[needs_source]]  phrases or a regex `pattern` that may only appear in a sentence that cites
                    one of the recording's approved sources
  [[disclosures]]   text every page must carry

Fixes never reword anyone. A sentence that needs a source gets one of the sources approved with the
recording (matched by its `supports` words), or it is removed. A sentence with a banned phrase is
removed. A missing disclosure is added. Anything else, such as a banned phrase in the question
itself, stays a finding, and a page with findings is never published.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .answers import RecordedAnswer
from .page import Page, Sentence, description_parts
from .text import clean, phrase_in, sha256


@dataclass
class Rule:
    id: str
    kind: str  # "banned" or "needs_source"
    reason: str
    phrases: list[str]
    pattern: re.Pattern[str] | None = None

    def hit(self, text: str) -> str | None:
        for p in self.phrases:
            m = re.search(r"(?<!\w)%s(?!\w)" % re.escape(p), text, re.I)
            if m:
                return m.group(0)
        m = self.pattern.search(text) if self.pattern else None
        return m.group(0) if m else None


@dataclass
class RulePack:
    name: str
    rules: list[Rule]
    disclosures: list[tuple[str, str]]
    sha256: str = ""


@dataclass
class Finding:
    rule: str
    kind: str  # banned, needs_source, missing_disclosure, structure
    reason: str
    where: str  # title, description, answer, transcript, page
    text: str
    match: str = ""

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def load_rules(path: Path) -> RulePack:
    raw = Path(path).read_bytes()
    data = tomllib.loads(raw.decode())
    rules = []
    for kind in ("banned", "needs_source"):
        for r in data.get(kind, []):
            if not r.get("id") or not (r.get("phrases") or r.get("pattern")):
                raise ValueError("every [[%s]] rule needs an id and phrases or a pattern" % kind)
            rules.append(Rule(str(r["id"]), kind, str(r.get("reason", "")), [clean(p) for p in r.get("phrases", [])],
                              re.compile(r["pattern"], re.I) if r.get("pattern") else None))
    disclosures = [(str(d["id"]), clean(str(d["text"]))) for d in data.get("disclosures", [])]
    return RulePack(str(data.get("name", Path(path).name)), rules, disclosures, sha256(raw))


def check(page: Page, pack: RulePack) -> list[Finding]:
    found: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    def description_is_cited(rule: Rule) -> bool:
        origins = page.description_from
        if not origins:
            derived, origins = description_parts(page.answer)
            if derived != page.description:
                origins = [page.description] if any(s.text == page.description for s in page.all_sentences()) else []
        if not origins or description_parts([Sentence(text) for text in origins]) != (page.description, origins):
            return False
        affected = [text for text in origins if rule.hit(text)]
        return bool(affected) and all(any(s.text == text and s.sources for s in page.all_sentences())
                                      for text in affected)

    def scan(text: str, where: str, kinds: tuple[str, ...], cited: bool = False) -> None:
        for rule in pack.rules:
            if rule.kind not in kinds or (rule.kind == "needs_source" and cited):
                continue
            hit = rule.hit(text)
            if hit and where == "description" and rule.kind == "needs_source" and description_is_cited(rule):
                continue
            if hit and (rule.id, text) not in seen:
                seen.add((rule.id, text))
                found.append(Finding(rule.id, rule.kind, rule.reason, where, text, hit))

    scan(page.question, "title", ("banned", "needs_source"))
    for name in ("speaker_name", "speaker_role", "site_name", "recorded_on", "published_on"):
        scan(getattr(page, name), name, ("banned", "needs_source"))
    for source in page.sources:
        scan(source["title"], "source", ("banned", "needs_source"))
    for disclosure in page.disclosures:
        scan(disclosure, "disclosure", ("banned", "needs_source"))
    for s in page.answer:
        scan(s.text, "answer", ("banned", "needs_source"), cited=bool(s.sources))
    for para in page.transcript:
        for s in para:
            scan(s.text, "transcript", ("banned", "needs_source"), cited=bool(s.sources))
    scan(page.description, "description", ("banned", "needs_source"))
    for did, text in pack.disclosures:
        if text not in page.disclosures:
            found.append(Finding(did, "missing_disclosure", "required disclosure", "page", text))
    if not page.answer:
        found.append(Finding("answer", "structure", "the answer section is empty", "page", ""))
    return found


def _short(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[:n - 3].rsplit(" ", 1)[0] + "..."


def _remove(page: Page, text: str) -> None:
    page.answer = [s for s in page.answer if s.text != text]
    page.transcript = [kept for kept in ([s for s in para if s.text != text] for para in page.transcript) if kept]
    page.removed.append(text)


def fix(page: Page, findings: list[Finding], answer: RecordedAnswer, pack: RulePack) -> list[str]:
    """Apply safe fixes in place and say what changed. Run `check` again afterwards."""
    actions: list[str] = []
    rederive = False
    disclosure_text = dict(pack.disclosures)
    for f in findings:
        if f.kind == "missing_disclosure":
            page.disclosures.append(disclosure_text[f.rule])
            actions.append("added disclosure %s" % f.rule)
        elif f.where == "description":
            rederive = True
        elif f.where in ("answer", "transcript") and f.text not in page.removed:
            source = next((s for s in answer.sources if s.supports and phrase_in(s.supports, f.text)), None)
            if f.kind == "needs_source" and source:
                urls = [s["url"] for s in page.sources]
                if source.url not in urls:
                    page.sources.append({"url": source.url, "title": source.title})
                    urls.append(source.url)
                n = urls.index(source.url) + 1
                for s in page.all_sentences():
                    if s.text == f.text and n not in s.sources:
                        s.sources.append(n)
                actions.append('cited approved source [%d] %s for "%s"' % (n, source.url, _short(f.text)))
            else:
                _remove(page, f.text)
                rederive = True
                why = 'banned phrase "%s"' % f.match if f.kind == "banned" else "no approved source"
                actions.append('removed "%s" (%s; a speaker is never reworded)' % (_short(f.text), why))
    if rederive:
        page.description, page.description_from = description_parts(page.answer)
        actions.append("rebuilt the meta description from the remaining answer")
    return actions
