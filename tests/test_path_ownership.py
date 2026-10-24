import copy
import datetime as dt
from urllib.parse import urlsplit

import pytest
from helpers import RULES, TODAY, ListSource, make_cfg, write_answer
from test_draft import FakeClient
from test_engine import REVIEW, SLUG, github, run

from seo_engine.claims import load_rules
from seo_engine.engine import Deps, Engine
from seo_engine.livecheck import Response
from seo_engine.page import build_page, kept_transcript
from seo_engine.planner import Action, plan
from seo_engine.state import State


def saved_page(root, owner, slug):
    answer = write_answer(root / owner, owner)
    page = build_page(answer, answer_sentences=kept_transcript(answer)[0][0][:1], slug=slug,
                      canonical="https://www.example.com/answers/%s/" % slug, site_name="Test Co",
                      published_on=TODAY.isoformat(), disclosures=[])
    return answer, page


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("status", ["published", "closed", "held", "dry-run", "rolled_back"])
def test_planner_refuses_an_occupied_fallback_in_every_page_status(tmp_path, status):
    answer = write_answer(tmp_path / "answers", "replacement")
    records = {SLUG: {"recording_id": "first"},
               SLUG + "-replacement": {"recording_id": "second", "status": status}}
    original = copy.deepcopy(records)
    result = plan([], [answer], records, [], threshold=0.6, rules_sha="rules", new_pages_left=1, today=TODAY)
    assert result.action is None and any(line.verdict == "refused" for line in result.lines)
    assert records == original


@pytest.mark.parametrize("owner", [None, "", [], {"recording": "first"}])
def test_unknown_recording_owners_are_reserved_without_breaking_planning(tmp_path, owner):
    answer = write_answer(tmp_path / "answers", "replacement")
    records = {slug: {"recording_id": owner} for slug in (SLUG, SLUG + "-replacement")}
    result = plan([], [answer], records, [], threshold=0.6, rules_sha="rules", new_pages_left=1, today=TODAY)
    assert result.action is None and result.blockers


@pytest.mark.parametrize("change", ["replace", "remove"])
def test_index_owner_cannot_be_reassigned_after_a_build_is_no_longer_live(tmp_path, change):
    state = State(tmp_path / "state")
    state.record(SLUG).update(recording_id="first", status="closed", live=False)
    state.save()
    before = snapshot(state.root)
    if change == "replace":
        state.records[SLUG]["recording_id"] = "replacement"
    else:
        state.records.pop(SLUG)
    with pytest.raises(PermissionError):
        state.save()
    assert snapshot(state.root) == before


def test_path_collision_is_reported_without_requesting_another_recording(tmp_path):
    state = State(tmp_path / "state")
    for owner, slug in (("first", SLUG), ("second", SLUG + "-replacement")):
        state.record(slug).update(recording_id=owner, status="closed")
    state.save()
    write_answer(tmp_path / "answers", "replacement")
    result, _ = run(tmp_path, mode="force")
    assert result["blockers"] and result["report"]["questions_to_record"] == 0
    assert "both page slug candidates" in result["report"]["text"]


@pytest.mark.parametrize("pending", [False, True])
def test_all_saved_builds_reserve_both_slug_candidates_without_index_entries(tmp_path, pending):
    state = State(tmp_path / "state")
    for owner, slug in (("first", SLUG), ("second", SLUG + "-replacement")):
        _, page = saved_page(tmp_path / "recordings", owner, slug)
        state.store_page(page, pending=pending)
    saved = snapshot(state.root)
    write_answer(tmp_path / "answers", "replacement")
    fake, review = github()
    result, _ = run(tmp_path, gh=review, review=REVIEW, mode="force")
    assert result["blockers"] and "report" in result and fake.pulls == {}
    for path, content in saved.items():
        assert (state.root / path).read_bytes() == content
    assert State(state.root).records == {}


@pytest.mark.parametrize("entry", ["build", "ship", "publish"])
@pytest.mark.parametrize("evidence", ["record", "published", "pending", "canonical"])
def test_forced_build_cannot_change_a_path_owner_before_any_side_effect(tmp_path, entry, evidence):
    _, previous = saved_page(tmp_path / "recordings", "first", SLUG)
    state = State(tmp_path / "state")
    if evidence in ("published", "pending"):
        state.store_page(previous, pending=evidence == "pending")
    else:
        key = SLUG if evidence == "record" else "different-slug"
        state.record(key).update(recording_id="first", url=previous.canonical, status="closed")
        state.save()
    answer, page = saved_page(tmp_path / "recordings", "replacement", SLUG)
    fake, review = github()
    client = FakeClient([{"text": kept_transcript(answer)[0][0][0], "from": [1]}])
    cfg = make_cfg(tmp_path, review=REVIEW, draft={"mode": "model"})
    engine = Engine(cfg, Deps(search=ListSource(), review=review, llm=client), today=TODAY)
    engine.answers, engine.pack = [answer], load_rules(RULES)
    engine.r["action"] = {"kind": "build"}
    before = snapshot(state.root)
    records = copy.deepcopy(engine.state.records)
    with pytest.raises(PermissionError):
        if entry == "build":
            engine.build(Action("build", SLUG, "approved recording", answer))
        elif entry == "ship":
            engine.ship(page, answer, new=True, title="Add recorded answer")
        else:
            engine.publish(page, engine.state.records.get(SLUG, {}), new=True)
    assert engine.state.records == records and snapshot(state.root) == before
    assert fake.calls == [] and client.calls == [] and not (tmp_path / "site").exists()


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("draft", [False, True])
def test_saved_build_and_draft_writers_cannot_replace_another_recording(tmp_path, pending, draft):
    state = State(tmp_path / "state")
    _, previous = saved_page(tmp_path / "recordings", "first", SLUG)
    state.store_page(previous, pending=pending)
    _, replacement = saved_page(tmp_path / "recordings", "replacement", SLUG)
    before = snapshot(state.root)
    with pytest.raises(PermissionError):
        if draft:
            state.store_draft(replacement)
        else:
            state.store_page(replacement, pending=not pending)
    assert snapshot(state.root) == before


def test_git_auto_merge_cannot_replace_the_owner_of_a_fallback_path(tmp_path):
    fake, review = github()

    def fetch(url):
        slug = urlsplit(url).path.strip("/").split("/")[-1]
        body = review.base_file("answers/%s/index.html" % slug)
        return Response(200 if body else 404, url, {}, body or "")

    settings = {"review": {**REVIEW, "auto_merge": True}, "publish": {"target": "git"},
                "livecheck": {"fetch_base": ""}}
    for offset, (owner, question) in enumerate([
        ("first", "Why does a tap drip?"), ("second", "Why does a tap drip slowly?"),
    ]):
        write_answer(tmp_path / "answers", owner, question)
        result, cfg = run(tmp_path, gh=review, fetch=fetch, today=TODAY + dt.timedelta(days=offset), **settings)
        assert result["status"] == "published" and not result["blockers"]
    victim = "why-does-a-tap-drip-slowly"
    saved = (cfg.state_dir / "pages" / (victim + ".json")).read_bytes()
    remote = review.base_file("answers/%s/index.html" % victim)
    write_answer(tmp_path / "answers", "slowly", "Why does a tap drip?")
    fake.calls.clear()
    result, _ = run(tmp_path, gh=review, fetch=fetch, today=TODAY + dt.timedelta(days=2), mode="force", **settings)
    assert result["blockers"] and result["status"] != "published" and "report" in result
    assert len(fake.pulls) == 2 and all(method == "GET" for method, _, _ in fake.calls)
    assert (cfg.state_dir / "pages" / (victim + ".json")).read_bytes() == saved
    assert review.base_file("answers/%s/index.html" % victim) == remote
    assert State(cfg.state_dir).records[victim]["recording_id"] == "second"
