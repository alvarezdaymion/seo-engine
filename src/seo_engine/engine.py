"""One engine run. Schedule `seo-engine run` once a day.

1. Read search data: the last `days` days of final Search Console data and the `days` before.
2. Live-check every page the engine has published. A failing page is the first thing to repair.
3. Rank opportunities and plan one action (planner.py). A query with no approved recorded answer
   is refused and goes into the report as a question to record.
4. Carry out the action: build from the approved recording, check every claim (apply the safe
   fixes, check again), then open a pull request, merge and publish. Each of those happens only
   when its switch is on and every gate passes. A publish is followed by a live check with
   retries, and by a rollback when the check still fails.
5. On the report weekday, write the weekly report, and send it when delivery is on.

A failing step is recorded as a blocker and never stops the steps after it.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from . import report as report_mod
from .answers import RecordedAnswer, load_answers
from .claims import check, fix, load_rules
from .config import WEEKDAYS
from .draft import Draft, make_draft
from .gates import Gate, content_gates, failed
from .http import Transport
from .livecheck import CheckResult, Fetch, check_until, expected_for, fetch_any
from .livecheck import check as live_check
from .opportunities import rank
from .page import Page, build_page, kept_transcript, render_body
from .planner import Action, held_key, plan
from .search import CsvSource, between, complete_weeks, latest_date, periods
from .state import RunLock, State, WriteJournal, read_json, write_json
from .text import content_words, dice, sha256

DESCRIPTION_MAX = 160


@dataclass
class Deps:
    """Connectors. Anything left as None is built from the config and the environment."""

    search: Any = None
    publisher: Any = None
    review: Any = None
    fetch: Fetch | None = None
    llm: Any = None
    webhook: Transport | None = None
    sleep: Callable[[float], None] = time.sleep


class Engine:
    def __init__(self, cfg: Any, deps: Deps | None = None, today: dt.date | None = None):
        self.cfg = cfg
        self.deps = deps or Deps()
        self.today = today or dt.date.today()
        self.state = State(cfg.state_dir)
        self.answers: list[RecordedAnswer] = []
        self.pack: Any = None
        self.rows: list[Any] = []
        self.coverage: set[str] = set()
        self.latest: dt.date | None = None
        self.search_error = ""
        self.opps: list[Any] = []
        self.plan: Any = None
        self.r: dict[str, Any] = {"today": self.today.isoformat(), "status": "idle", "blockers": [], "action": None}

    # ---- connectors ----------------------------------------------------------------------

    def publisher(self) -> Any:
        if self.deps.publisher is None:
            from .publish import build_publisher
            self.deps.publisher = build_publisher(self.cfg)
        return self.deps.publisher

    def review(self) -> Any:
        rc = self.cfg["review"]
        if not rc["enabled"]:
            return None
        if self.deps.review is None:
            from .review import GitHubReview
            self.deps.review = GitHubReview(rc["repo"], rc["base"], rc["path"], rc["branch_prefix"], rc["merge_method"])
        if not getattr(self.deps.review, "journal", None) or self.deps.review.journal.path is None:
            self.deps.review.journal = WriteJournal(self.cfg.state_dir / "github-writes.json")
        return self.deps.review

    def search_source(self) -> Any:
        if self.deps.search is None:
            s = self.cfg["search"]
            if s["source"] == "csv":
                self.deps.search = CsvSource(self.cfg.path(s["csv"]))
            else:
                from .searchconsole import SearchConsoleSource
                self.deps.search = SearchConsoleSource(s["site_url"], country=s["country"])
        return self.deps.search

    def fetch_url(self, page: Page) -> str:
        base = self.cfg["livecheck"]["fetch_base"]
        if not base:
            return page.canonical
        if base == "file":
            return str(self.cfg.path(self.cfg["static"]["dir"]) / self.cfg["static"]["file"].format(slug=page.slug))
        return base.rstrip("/") + self.cfg["site"]["page_path"].format(slug=page.slug)

    def blocker(self, message: str) -> None:
        self.r["blockers"].append(message)

    def answer(self, recording_id: str) -> RecordedAnswer | None:
        return next((a for a in self.answers if a.id == recording_id), None)

    def release_gates(self, page: Page) -> list[Gate]:
        self.answers, errors = load_answers(self.cfg.path(self.cfg["answers"]["dir"]))
        try:
            self.pack = load_rules(self.cfg.path(self.cfg["claims"]["rules"]))
        except Exception:
            return [Gate("claims", False, "current rule pack could not be loaded")]
        gates = content_gates(page, self.answers, check(page, self.pack))
        try:
            self.state.require_owner(page.slug, page.recording_id, page.canonical)
        except PermissionError as e:
            gates.append(Gate("ownership", False, str(e)))
        else:
            gates.append(Gate("ownership", True, "page path belongs to this recording"))
        if errors:
            gates.append(Gate("recordings", False, "current recorded answers could not all be loaded"))
        return gates

    def require_release(self, page: Page) -> None:
        problems = failed(self.release_gates(page))
        if problems:
            raise RuntimeError("release gate failed: " + "; ".join(g.detail for g in problems))

    # ---- the run -------------------------------------------------------------------------

    def run(self, execute: bool = True, report: str = "auto", send_report: bool | None = None) -> dict[str, Any]:
        try:
            with RunLock(self.cfg.state_dir / "run.lock"):
                self.state = State(self.cfg.state_dir)
                return self._run(execute, report, send_report)
        except RuntimeError as e:
            self.blocker(str(e))
            self.r["status"] = "error"
            return self.r

    def _run(self, execute: bool, report: str, send_report: bool | None) -> dict[str, Any]:
        self.answers, errors = load_answers(self.cfg.path(self.cfg["answers"]["dir"]))
        for e in errors:
            self.blocker("recorded answer skipped: " + e)
        try:
            self.pack = load_rules(self.cfg.path(self.cfg["claims"]["rules"]))
        except Exception as e:
            self.blocker("rule pack did not load (%s); nothing is built or published" % str(e)[:200])
        self.read_search()
        failing = self.live_gate()
        current, previous = self.windows()
        self.opps = rank(current, previous, self.cfg["opportunities"], self.cfg["site"]["brand_terms"],
                         self.state.published_urls())
        left = int(self.cfg["publish"]["max_new_pages_per_day"]) - self.state.new_pages_on(self.today)
        self.plan = plan(self.opps, self.answers, self.state.records, failing,
                         threshold=float(self.cfg["answers"]["match_threshold"]),
                         rules_sha=self.pack.sha256 if self.pack else "", new_pages_left=left, today=self.today,
                         check_slug=lambda slug, owner: self.state.require_owner(slug, owner, self.cfg.page_url(slug)))
        for problem in self.plan.blockers:
            self.blocker(problem)
        if self.plan.blockers and self.plan.action is None:
            self.r["status"] = "held"
        self.r["opportunities"] = [o.to_json() for o in self.opps[:10]]
        self.r["plan"] = {"lines": [ln.to_json() for ln in self.plan.lines], "note": self.plan.note,
                          "action": self.plan.action.kind if self.plan.action else None}
        if execute and self.plan.action and self.pack:
            try:
                {"finish": self.finish, "repair": self.repair, "build": self.build,
                 "snippet": self.snippet}[self.plan.action.kind](self.plan.action)
            except Exception as e:
                self.blocker("%s %s failed: %s: %s" % (self.plan.action.kind, self.plan.action.slug,
                                                         type(e).__name__, str(e)[:300]))
                self.r["status"] = "error"
        try:
            if report == "force" or (report == "auto" and self.report_due()):
                self.write_report(mark=report == "auto", send=send_report)
        except Exception as e:
            self.blocker("weekly report failed: %s" % str(e)[:300])
        self.state.save()
        write_json(self.cfg.state_dir / "last-run.json", self.r)
        return self.r

    def read_search(self) -> None:
        days = int(self.cfg["search"]["days"])
        self.rows, self.latest, self.coverage, self.search_error = [], None, set(), ""
        try:
            start = min(self.today - dt.timedelta(days=2 * days + 7), complete_weeks(self.today)[1][0])
            self.rows = self.search_source().fetch(start, self.today)
            self.coverage = set(getattr(self.rows, "covered_dates", (r.date for r in self.rows)))
            for date in self.coverage | {r.date for r in self.rows}:
                dt.date.fromisoformat(date)
            self.latest = latest_date(self.rows)
        except Exception as e:
            self.rows, self.latest, self.coverage = [], None, set()
            self.search_error = "%s: %s" % (type(e).__name__, str(e)[:240])
            self.blocker("search data unavailable: " + self.search_error)
        self.r["search"] = {"rows": len(self.rows), "latest": self.latest.isoformat() if self.latest else None,
                            "error": self.search_error, "days": days}

    def windows(self) -> tuple[list[Any], list[Any]]:
        if not self.latest:
            return [], []
        cur, prev = periods(self.latest, int(self.cfg["search"]["days"]))
        self.r["search"].update(current=[d.isoformat() for d in cur], previous=[d.isoformat() for d in prev])
        return between(self.rows, *cur), between(self.rows, *prev)

    def live_gate(self) -> list[str]:
        failing, out = [], []
        fetch = self.deps.fetch or fetch_any
        for slug, rec in sorted(self.state.records.items()):
            if not rec.get("live"):
                continue
            try:
                page = self.state.page(slug)
                if page is None:
                    raise RuntimeError("saved build is missing")
            except Exception as e:
                problem = "saved build is unavailable (%s)" % type(e).__name__
                self.blocker("%s: %s" % (slug, problem))
                res = CheckResult(False, rec.get("url", ""), problems=[problem])
            else:
                try:
                    res = live_check(expected_for(page, self.fetch_url(page)), fetch)
                except Exception as e:
                    res = CheckResult(False, self.fetch_url(page),
                                      problems=["live check failed (%s)" % type(e).__name__])
            rec["last_check"] = {"ok": res.ok, "problems": res.problems, "on": self.today.isoformat()}
            out.append({"slug": slug, "url": res.url, "ok": res.ok, "problems": res.problems})
            if not res.ok:
                failing.append(slug)
        self.r["live_gate"] = out
        return failing

    # ---- actions -------------------------------------------------------------------------

    def build(self, a: Action) -> None:
        answer = a.answer
        assert answer is not None
        self.state.require_owner(a.slug, answer.id, self.cfg.page_url(a.slug))
        kept, cut = kept_transcript(answer)
        flat = [s for para in kept for s in para]
        def draft_once() -> Draft:
            return make_draft(answer.question, flat, self.cfg["draft"],
                              int(self.cfg["answers"]["answer_sentences"]), self.deps.llm)

        if self.cfg["draft"]["mode"] == "model":
            key = sha256(json.dumps([answer.revision_sha256, self.cfg["draft"],
                                     self.cfg["answers"]["answer_sentences"]], sort_keys=True))
            draft = Draft(**WriteJournal(self.cfg.state_dir / "model-drafts.json").perform(
                key, lambda: asdict(draft_once())))
        else:
            draft = draft_once()
        rec = self.state.records.get(a.slug, {})
        page = build_page(answer, answer_sentences=draft.sentences, slug=a.slug, canonical=self.cfg.page_url(a.slug),
                          site_name=self.cfg["site"]["name"],
                          published_on=rec.get("published_on") or self.today.isoformat(),
                          disclosures=[t for _, t in self.pack.disclosures], draft_method=draft.method)
        o = a.opportunity
        evidence = ('query "%s": %s impressions, position %.1f' % (o.query, format(int(o.impressions), ","), o.position)
                    if o else "approved recording, no search data for it yet")
        self.r["action"] = {"kind": "build", "slug": a.slug, "url": page.canonical, "recording": answer.id,
                            "speaker": answer.speaker_name, "approved_on": answer.approval.get("on", ""),
                            "evidence": evidence, "draft": draft.method, "draft_note": draft.note,
                            "answer_sentences": len(draft.sentences), "cut_sentences": cut}
        self.state.log({"event": "built", "slug": a.slug, "recording": answer.id, "draft": draft.method})
        self.ship(page, answer, new=not rec.get("live"), title="Add answer page: %s" % answer.question)

    def snippet(self, a: Action) -> None:
        page, rec = self.state.page(a.slug), self.state.records[a.slug]
        answer = self.answer(rec.get("recording_id", ""))
        query = a.opportunity.query if a.opportunity else ""
        self.r["action"] = {"kind": "snippet", "slug": a.slug, "url": rec.get("url"), "query": query}
        if page is None or answer is None:
            self.r["action"]["status"] = self.r["status"] = "held"
            self.blocker("snippet %s: the published build or its recording is missing" % a.slug)
            return
        words = content_words(query)
        candidates = [s.text for para in page.transcript for s in para
                      if len(s.text) <= DESCRIPTION_MAX and not s.sources and s.text not in page.description]
        scored = sorted(((dice(words, content_words(t)), t) for t in candidates), key=lambda x: -x[0])
        if not scored or scored[0][0] == 0:
            self.r["action"]["status"] = "no better description in the transcript"
            return
        self.r["action"]["description"] = {"before": page.description, "after": scored[0][1]}
        page.description = scored[0][1]
        page.description_from = [scored[0][1]]
        self.ship(page, answer, new=False, title="Update description: %s" % page.question)

    def ship(self, page: Page, answer: RecordedAnswer, *, new: bool, title: str) -> None:
        self.state.require_owner(page.slug, page.recording_id, page.canonical)
        if page.recording_id != answer.id:
            raise PermissionError("the build and recording owners do not match")
        act = self.r["action"]
        first = check(page, self.pack)
        findings, fixes = first, []
        if first:
            fixes = fix(page, first, answer, self.pack)
            findings = check(page, self.pack)
        act["claims"] = {"rules": self.pack.name, "first": [f.to_json() for f in first], "fixes": fixes,
                         "final": [f.to_json() for f in findings]}
        act["draft_file"] = str(self.state.store_draft(page))
        rec = self.state.record(page.slug)
        rec.update(url=page.canonical, recording_id=answer.id, transcript_sha256=answer.transcript_sha256)
        gates = self.release_gates(page)
        act["gates"] = [g.to_json() for g in gates]
        if failed(gates):
            reason = "; ".join("%s: %s" % (g.name, g.detail) for g in failed(gates))
            rec.update(status="held", reason=reason, held_key=held_key(answer, self.pack.sha256))
            act["status"] = self.r["status"] = "held"
            self.blocker("held %s: %s" % (page.slug, reason))
            self.state.log({"event": "held", "slug": page.slug, "reason": reason})
            return
        self.state.store_page(page, pending=True)
        rec["pending_kind"] = act["kind"]
        rec["pending_key"] = held_key(answer, self.pack.sha256)
        review = self.review()
        if review is None:
            self.publish(page, rec, new=new)
            return
        previous = self.state.page(page.slug)
        rec.update(status="opening_review", new=new, opening={
            "files": self.publisher().files(page), "title": title, "body": self.pr_body(page, answer, act),
            "previous": self.publisher().files(previous) if previous else {}})
        self.state.save()
        self.open_review(page, rec)

    def open_review(self, page: Page, rec: dict[str, Any]) -> None:
        self.state.require_owner(page.slug, page.recording_id, page.canonical)
        act, review, opening = self.r["action"], self.review(), rec["opening"]
        title = opening["title"]
        pr = review.open_pull_request(page.slug, opening["files"], title, opening["body"], opening["previous"])
        pr["title"] = title
        rec.update(status="in_review", pr=pr, reason="")
        self.state.save()
        act["review"] = {"status": "opened", **pr}
        self.state.log({"event": "opened", "slug": page.slug, "pr": pr["number"], "branch": pr["branch"]})
        self.try_merge(page, rec)

    def try_merge(self, page: Page, rec: dict[str, Any]) -> None:
        self.state.require_owner(page.slug, page.recording_id, page.canonical)
        act, review, rc = self.r["action"], self.review(), self.cfg["review"]
        pr = rec["pr"]
        gates = review.gates(pr["number"], pr["head"], pr["files"], list(rc["required_checks"]),
                             base_files=pr.get("base_files"), branch=pr["branch"])
        gates += self.release_gates(page)
        gates.append(Gate("auto-merge", bool(rc["auto_merge"]), "off: a person merges the pull request"))
        gates.append(Gate("publish mode", self.cfg["publish"]["mode"] == "live", "dry-run: nothing is merged"))
        act["merge_gates"] = [g.to_json() for g in gates]
        act.setdefault("review", {"status": "open", **pr})
        if failed(gates):
            rec["reason"] = "; ".join("%s: %s" % (g.name, g.detail) for g in failed(gates))
            act["review"]["status"] = "waiting"
            act["status"] = self.r["status"] = "in_review"
            return
        review.before_merge = lambda: self.require_release(page)
        sha = review.merge(pr["number"], pr["head"], "%s (#%d)" % (pr.get("title", page.question), pr["number"]),
                           branch=pr["branch"])
        rec.update(status="merged", merge_sha=sha, reason="")
        act["review"]["status"] = "merged"
        self.state.log({"event": "merged", "slug": page.slug, "pr": pr["number"], "sha": sha})
        self.publish(page, rec, new=bool(rec.get("new")))

    def finish(self, a: Action) -> None:
        rec = self.state.records[a.slug]
        page = self.state.page(a.slug, pending=True)
        self.r["action"] = {"kind": "finish", "slug": a.slug, "url": rec.get("url"), "pr": rec.get("pr")}
        if page is None:
            rec.update(status="held", reason="the pending build is missing from state/")
            self.r["action"]["status"] = self.r["status"] = "held"
            self.blocker("finish %s: the pending build is missing" % a.slug)
            return
        self.state.require_owner(page.slug, page.recording_id, page.canonical)
        pending_key = rec.get("pending_key")
        if pending_key is None:
            answer = self.answer(page.recording_id)
            pending_key = (held_key(answer, self.pack.sha256)
                           if answer and answer.revision_sha256 == page.revision_sha256
                           else "%s::%s" % (page.revision_sha256, self.pack.sha256))
        if rec["status"] == "merged":
            self.publish(page, rec, new=bool(rec.get("new")))
            return
        if rec["status"] == "opening_review":
            self.open_review(page, rec)
            return
        review = self.review()
        if review is None:
            self.blocker("finish %s: review is off but a pull request is open" % a.slug)
            return
        pr = review.pull(rec["pr"]["number"])
        if not review.target_ok(pr, rec["pr"]["branch"]):
            self.blocker("finish %s: pull request repository or target branch changed or is missing" % a.slug)
            self.r["status"] = "held"
            return
        if pr.get("merged"):
            changed = [p for p, c in self.publisher().files(page).items() if review.base_file(p) != c]
            if changed:
                reason = "the merged files differ from the approved build: %s" % ", ".join(changed)
                rec.update(status="held", reason=reason, held_key=pending_key)
                self.r["action"]["status"] = self.r["status"] = "held"
                self.blocker("held %s: %s" % (a.slug, reason))
                return
            rec.update(status="merged", reason="")
            self.state.log({"event": "merged", "slug": a.slug, "pr": rec["pr"]["number"], "by": "a person"})
            self.publish(page, rec, new=bool(rec.get("new")))
        elif pr.get("state") == "closed":
            rec.update(status="closed", reason="pull request closed without merging",
                       held_key=pending_key)
            self.r["action"]["status"] = self.r["status"] = "closed"
            self.state.log({"event": "closed", "slug": a.slug, "pr": rec["pr"]["number"]})
        else:
            self.try_merge(page, rec)

    def repair(self, a: Action) -> None:
        page, rec = self.state.page(a.slug), self.state.records[a.slug]
        self.r["action"] = {"kind": "repair", "slug": a.slug, "url": rec.get("url"),
                            "problems": (rec.get("last_check") or {}).get("problems", [])}
        if page is None:
            self.blocker("repair %s: the published build is missing from state/" % a.slug)
            return
        if self.publisher().target == "git":
            self.r["action"]["status"] = "reported"
            self.blocker("%s fails its live check; the site deploys from the repo, so fix it there" % rec.get("url"))
            return
        self.publish(page, rec, new=False, rollback=False)

    # ---- publish -------------------------------------------------------------------------

    def publish(self, page: Page, rec: dict[str, Any], *, new: bool, rollback: bool = True) -> None:
        self.state.require_owner(page.slug, page.recording_id, page.canonical)
        if rec.get("recording_id") not in (None, page.recording_id):
            raise PermissionError("the publication record belongs to another recording")
        act, lc = self.r["action"], self.cfg["livecheck"]
        gates = self.release_gates(page)
        act["publish_gates"] = [g.to_json() for g in gates]
        if failed(gates):
            reason = "; ".join("%s: %s" % (g.name, g.detail) for g in failed(gates))
            rec.update(status="held", reason=reason)
            act["status"] = self.r["status"] = "held"
            self.blocker("held %s before publishing: %s" % (page.slug, reason))
            return
        publisher = self.publisher()
        files = sorted(publisher.files(page))
        if self.cfg["publish"]["mode"] != "live":
            act["publish"] = {"status": "dry-run", "files": files}
            if not rec.get("live") and rec.get("status") != "merged":
                rec.update(status="dry-run")
            act["status"] = self.r["status"] = "dry-run"
            return
        publication = rec.get("publication")
        if publication is not None and (not isinstance(publication, dict) or publication.get("status") != "done"):
            raise RuntimeError("previous publication outcome is uncertain; reconcile state before publishing")
        rec["publication"] = {"status": "pending", "undo": {}}
        self.state.save()

        def save_recovery(undo: dict[str, Any]) -> None:
            rec["publication"]["undo"] = undo
            self.state.save()

        def require_restore(undo: dict[str, Any]) -> None:
            previous = self.state.page(page.slug)
            if publisher.target == "static":
                if previous is None or publisher.files(previous).get(undo.get("relative")) != undo.get("previous"):
                    raise RuntimeError("previous approved build cannot be verified; kept unpublished")
            elif (previous is None or undo.get("body_html") != render_body(previous)
                    or undo.get("title") != previous.question
                    or {k: value[1] for k, value in undo.get("seo", {}).items()}
                    != {"title_tag": previous.question, "description_tag": previous.description}):
                raise RuntimeError("previous approved build cannot be verified; kept hidden")
            self.require_release(previous)

        publisher.save_recovery = save_recovery
        publisher.before_write = lambda: self.require_release(page)
        publisher.before_restore = require_restore
        if publisher.target == "static":
            ownership = rec.get("ownership") or {}
            previous = self.state.page(page.slug)
            publisher.owned_files = {}
            if (rec.get("live") and previous and previous.recording_id == page.recording_id
                    and ownership.get("root") == str(publisher.root)
                    and ownership.get("recording_id") == page.recording_id
                    and isinstance(ownership.get("files"), dict)):
                publisher.owned_files = dict(ownership["files"])
        try:
            pub = publisher.publish(page)
        except Exception as e:
            if getattr(e, "recovery", None):
                act["rollback"] = e.recovery
            self.state.save()
            raise
        save_recovery(pub.undo)
        act["publish"] = {"status": pub.action, "where": pub.where, "files": files}
        try:
            res = check_until(expected_for(page, self.fetch_url(page)), self.deps.fetch or fetch_any,
                              int(lc["attempts"]), float(lc["wait_seconds"]), self.deps.sleep)
        except Exception as e:
            res = CheckResult(False, self.fetch_url(page), problems=["live check failed (%s)" % type(e).__name__])
        act["live"] = res.to_json()
        last_check = {"ok": res.ok, "problems": res.problems, "on": self.today.isoformat()}
        if publisher.target == "static":
            publisher.verify(pub)
        if res.ok:
            first, today = not rec.get("live"), self.today.isoformat()
            rec.update(status="published", live=True, reason="", last_check=last_check, updated_on=today,
                       published_on=rec.get("published_on") or today)
            rec["publication"]["status"] = "done"
            if publisher.target == "static":
                rec["ownership"] = {"root": str(publisher.root), "recording_id": page.recording_id,
                                    "files": {pub.binding["relative"]: pub.binding["path"]}}
            if rec.get("pending_kind") == "snippet":
                rec["snippet_on"] = today
            rec.pop("pending_kind", None)
            self.state.store_page(page)
            self.state.log({"event": "published", "slug": page.slug, "url": page.canonical, "new": new and first,
                            "day": self.today.isoformat(), "action": pub.action})
            act["status"] = self.r["status"] = "published"
            return
        problems = "; ".join(res.problems)
        if publisher.target == "git":
            rec["publication"]["status"] = "done"
            rec.update(status="merged", reason="merged but not live yet: %s" % problems, last_check=last_check)
            act["status"] = self.r["status"] = "not live"
            self.blocker("%s is merged but not live yet: %s" % (page.canonical, problems))
            return
        undo = publisher.rollback(pub) if rollback else "left as published (the page was already failing)"
        if pub.undo.get("unpublished"):
            rec["live"] = False
        rec["publication"]["status"] = "done"
        act["rollback"] = undo
        rec.update(status="rolled_back" if rollback else rec.get("status", "published"),
                   reason="live check failed: %s" % problems, last_check=last_check)
        act["status"] = self.r["status"] = "rolled back" if rollback else "still failing"
        self.blocker("live check failed for %s: %s; %s" % (page.canonical, problems, undo))
        self.state.log({"event": "rolled_back" if rollback else "repair_failed", "slug": page.slug,
                        "problems": res.problems})

    def pr_body(self, page: Page, answer: RecordedAnswer, act: dict[str, Any]) -> str:
        claims = act["claims"]
        lines = ['Built by seo-engine from recorded answer `%s`: "%s", answered by %s.'
                 % (answer.id, answer.question, answer.speaker_name), "",
                 "- Recording revision: %s" % page.revision_sha256,
                 "- Approved on: %s" % answer.approval.get("on", ""),
                 "- Evidence: %s" % act.get("evidence", act.get("query", "")),
                 "- Page: %s" % page.canonical,
                 "- Draft: %s%s" % (act.get("draft", "unchanged"),
                                    " (%s)" % act["draft_note"] if act.get("draft_note") else ""),
                 "- Claim check (%s): %s" % (claims["rules"], "passed" if not claims["first"]
                                             else "%d finding(s) before fixes, %d after"
                                             % (len(claims["first"]), len(claims["final"])))]
        lines += ["  - %s" % f for f in claims["fixes"]]
        lines.append("- Gates: " + ", ".join("%s %s" % (g["name"], "ok" if g["ok"] else "failed")
                                             for g in act["gates"]))
        return "\n".join(lines) + "\n"

    # ---- report --------------------------------------------------------------------------

    def report_due(self) -> bool:
        week = complete_weeks(self.today)[0][0].isoformat()
        last = read_json(self.cfg.state_dir / "report.json")
        if last is not None and not isinstance(last, dict):
            raise RuntimeError("report checkpoint must be an object")
        last = last or {}
        return self.today.weekday() >= WEEKDAYS.index(self.cfg["report"]["weekday"]) and last.get("week") != week

    def write_report(self, mark: bool = True, send: bool | None = None) -> dict[str, Any]:
        text, meta = report_mod.build(self.cfg, self.rows, self.latest, self.today, self.state.records,
                                      self.state.events(), self.plan, self.opps, self.answers, self.search_error,
                                      coverage=self.coverage)
        path = self.cfg.reports_dir / ("%s.md" % meta["week"][0])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        meta.update(path=str(path), sent=False, text=text)
        self.r["report"] = meta
        if send if send is not None else self.cfg["report"]["send"] == "webhook":
            url = report_mod.webhook_url()
            journal = WriteJournal(self.cfg.state_dir / "report-deliveries.json")
            journal.perform(meta["week"][0], lambda: report_mod.send_webhook(
                text, self.cfg["report"]["payload_key"], url=url, transport=self.deps.webhook))
            meta["sent"] = True
        if mark:
            write_json(self.cfg.state_dir / "report.json", {"week": meta["week"][0], "written": self.today.isoformat()})
        self.r["report"] = meta
        return meta
