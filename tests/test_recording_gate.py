"""The showcase guardrail: no page exists unless an approved recorded answer is behind it.

Version one of this system filled a daily article quota with model-written pages that nobody
clicked. These tests pin the fix: the planner refuses, the builder refuses, the gates before a
merge or publish refuse, and there is no setting that makes the engine produce pages on its own.
"""

import datetime as dt
import json

import pytest
from helpers import RULES, SITE, TODAY, ListSource, daily, make_cfg, write_answer

from seo_engine import config
from seo_engine.answers import NoApprovedRecording, approval_problems, approve, load_answer
from seo_engine.claims import load_rules
from seo_engine.engine import Deps, Engine
from seo_engine.gates import recording_gate
from seo_engine.livecheck import fetch_any
from seo_engine.opportunities import Opportunity
from seo_engine.page import build_page, kept_transcript
from seo_engine.planner import plan


def no_page(query, impressions=900, position=22.0):
    return Opportunity("no_page", query, SITE + "/", impressions, 2, position, 50.0)


def plan_for(opps, answers, records=None, left=1):
    return plan(opps, answers, records or {}, [], threshold=0.6, rules_sha="r", new_pages_left=left, today=TODAY)


def build(answer, sentences=None):
    kept, _ = kept_transcript(answer) if answer else ([["x"]], 0)
    return build_page(answer, answer_sentences=sentences or kept[0][:2], slug="s", canonical=SITE + "/answers/s/",
                      site_name="Test Co", published_on="2026-03-12", disclosures=[])


# ---- the planner -------------------------------------------------------------------------

def test_planner_refuses_a_query_that_no_recording_covers(tmp_path):
    answer = write_answer(tmp_path)
    p = plan_for([no_page("how to relight a pilot light")], [answer])
    assert p.action.kind == "build" and p.action.answer.id == "ra-100"  # the approved answer still gets its page
    line = p.lines[0]
    assert line.verdict == "refused" and "no recorded answer" in line.detail
    assert [ln.target for ln in p.refused] == ['"how to relight a pilot light"']


def test_planner_with_no_recordings_at_all_does_nothing():
    p = plan_for([no_page("kitchen faucet dripping"), no_page("water heater popping noise")], [])
    assert p.action is None
    assert len(p.refused) == 2
    assert "nothing to do" in p.note


def test_planner_refuses_a_matching_recording_that_is_not_approved(tmp_path):
    answer = write_answer(tmp_path, approved=False)
    p = plan_for([no_page("why is my kitchen faucet dripping")], [answer])
    assert p.action is None
    assert p.lines[0].verdict == "refused" and "not approved" in p.lines[0].detail


def test_planner_refuses_when_the_transcript_changed_after_approval(tmp_path):
    answer = write_answer(tmp_path)
    (tmp_path / "ra-100.txt").write_text("A different answer that nobody approved. It adds new claims.")
    p = plan_for([no_page("why is my kitchen faucet dripping")], [load_answer(tmp_path / "ra-100.json")])
    assert p.action is None
    assert "transcript changed after approval" in p.lines[0].detail
    assert answer.approved  # the copy loaded before the edit was approved; the reload is not


def test_planner_builds_only_from_an_approved_matching_recording(tmp_path):
    answer = write_answer(tmp_path)
    p = plan_for([no_page("kitchen faucet dripping")], [answer])
    assert p.action.kind == "build" and p.action.answer is answer
    assert p.lines[0].chosen and p.lines[0].verdict == "build"


def test_a_page_count_setting_is_only_a_ceiling(tmp_path):
    answer = write_answer(tmp_path)
    capped = plan_for([no_page("kitchen faucet dripping")], [answer], left=0)
    assert capped.action is None and "cap reached" in capped.note
    assert plan_for([no_page("kitchen faucet dripping")], [], left=50).action is None


# ---- the builder -------------------------------------------------------------------------

def test_builder_refuses_without_a_recording():
    with pytest.raises(NoApprovedRecording):
        build_page(None, answer_sentences=["Anything at all."], slug="s", canonical=SITE + "/s/", site_name="Test Co",
                   published_on="2026-03-12", disclosures=[])


@pytest.mark.parametrize("change,problem", [
    ({"approval": {"status": "recorded"}}, "not approved"),
    ({"approval": {"status": "approved", "transcript_sha256": "0" * 64}}, "transcript changed after approval"),
    ({"video_url": ""}, "no https video URL"),
    ({"video_url": "http://videos.example.com/a.mp4"}, "no https video URL"),
    ({"speaker": {"name": " "}}, "no speaker name"),
])
def test_builder_refuses_a_recording_that_cannot_back_a_page(tmp_path, change, problem):
    write_answer(tmp_path)
    meta = json.loads((tmp_path / "ra-100.json").read_text())
    meta.update(change)
    (tmp_path / "ra-100.json").write_text(json.dumps(meta))
    answer = load_answer(tmp_path / "ra-100.json")
    assert any(problem in p for p in approval_problems(answer))
    with pytest.raises(NoApprovedRecording, match=problem):
        build(answer)


def test_builder_refuses_answer_text_that_is_not_in_the_transcript(tmp_path):
    answer = write_answer(tmp_path)
    with pytest.raises(ValueError, match="not in the approved transcript"):
        build(answer, ["A dripping faucet is guaranteed to stop after one visit."])


def test_builder_leaves_out_lines_cut_from_the_approved_edit(tmp_path):
    answer = write_answer(tmp_path, cut=["Do not overtighten the handle, because that only crushes the washer."])
    kept, cut = kept_transcript(answer)
    text = " ".join(s for para in kept for s in para)
    assert cut == 1 and "overtighten" not in text and "cartridge" in text


def test_approval_is_bound_to_the_transcript(tmp_path):
    write_answer(tmp_path, approved=False)
    approved = approve(tmp_path / "ra-100.json", "owner", TODAY)
    assert approved.approved and approved.approval["on"] == TODAY.isoformat()
    (tmp_path / "ra-100.txt").write_text("Edited after approval.")
    assert not load_answer(tmp_path / "ra-100.json").approved


# ---- the gates before merge and publish ----------------------------------------------------

def test_release_gate_rechecks_the_approval_at_merge_and_publish_time(tmp_path):
    answer = write_answer(tmp_path)
    page = build(answer)
    assert recording_gate(page, [answer]).ok
    meta = json.loads((tmp_path / "ra-100.json").read_text())
    meta["approval"]["status"] = "withdrawn"
    (tmp_path / "ra-100.json").write_text(json.dumps(meta))
    gate = recording_gate(page, [load_answer(tmp_path / "ra-100.json")])
    assert not gate.ok and "not approved" in gate.detail
    assert not recording_gate(page, []).ok  # the recording is gone


# ---- a whole run -------------------------------------------------------------------------

def test_a_run_without_approved_recordings_publishes_nothing_and_asks_for_recordings(tmp_path):
    write_answer(tmp_path / "answers", approved=False)
    end = TODAY - dt.timedelta(days=2)
    rows = daily("why is my kitchen faucet dripping", "/", 900, 2, 21.0, end) + \
        daily("how to unclog a shower drain", "/", 700, 1, 24.0, end)
    cfg = make_cfg(tmp_path, publish={"max_new_pages_per_day": 10})
    deps = Deps(search=ListSource(rows), fetch=fetch_any, sleep=lambda s: None)
    r = Engine(cfg, deps, today=TODAY).run(report="force")
    assert r["status"] == "idle" and r["action"] is None
    assert not (tmp_path / "site").exists()
    text = r["report"]["text"]
    assert '"how to unclog a shower drain"' in text.split("## Questions to record")[1]
    assert "ra-100" in text.split("## Recordings waiting for approval")[1]


def test_the_config_has_no_page_quota():
    flat = json.dumps(config.DEFAULTS)
    assert "quota" not in flat and "min_pages" not in flat
    assert config.DEFAULTS["publish"]["max_new_pages_per_day"] == 1


def test_example_rule_pack_loads():
    pack = load_rules(RULES)
    assert pack.rules and pack.disclosures and len(pack.sha256) == 64


def test_two_recordings_with_the_same_question_never_share_a_page(tmp_path):
    first = write_answer(tmp_path, "ra-100")
    second = write_answer(tmp_path, "ra-200")
    records = {"why-is-my-kitchen-faucet-dripping": {"recording_id": "ra-100", "live": True,
                                                     "url": SITE + "/answers/why-is-my-kitchen-faucet-dripping/"}}
    p = plan_for([], [first, second], records)
    assert p.action.answer is second and p.action.slug == "why-is-my-kitchen-faucet-dripping-ra-200"
