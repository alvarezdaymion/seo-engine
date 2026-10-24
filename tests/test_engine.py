import datetime as dt
import json

import pytest
from helpers import TODAY, ListSource, daily, make_cfg, write_answer

from seo_engine import demo
from seo_engine.demo import MemoryGitHub, run_demo
from seo_engine.engine import Deps, Engine
from seo_engine.livecheck import Response, fetch_any
from seo_engine.report import send_webhook
from seo_engine.review import GitHubReview

END = TODAY - dt.timedelta(days=2)
SLUG = "why-is-my-kitchen-faucet-dripping"


def run(tmp_path, rows=(), fetch=fetch_any, gh=None, today=TODAY, webhook=None, mode="off", **sections):
    cfg = make_cfg(tmp_path, **sections)
    deps = Deps(search=ListSource(list(rows)) if not isinstance(rows, ListSource) else rows, review=gh,
                fetch=fetch, webhook=webhook, sleep=lambda s: None)
    return Engine(cfg, deps, today=today).run(report=mode), cfg


def page_file(tmp_path, slug=SLUG):
    return tmp_path / "site" / "answers" / slug / "index.html"


def github():
    fake = MemoryGitHub()
    return fake, GitHubReview("example/site", token="test-token-not-real", transport=fake, sleep=lambda s: None)


REVIEW = {"enabled": True, "repo": "example/site", "auto_merge": False}


# ---- the demo ----------------------------------------------------------------------------

def test_demo_runs_end_to_end_offline(tmp_path):
    result, cfg, server = run_demo(tmp_path / "demo")
    try:
        assert result["status"] == "published" and not result["blockers"]
        verdicts = {ln["target"]: ln["verdict"] for ln in result["plan"]["lines"]}
        assert verdicts['"how to relight a pilot light"'] == "refused"
        assert verdicts['"tankless water heater installation cost"'] == "refused"
        assert verdicts['"water heater popping noise"'] == "build"
        action = result["action"]
        assert [f["kind"] for f in action["claims"]["first"]] == ["needs_source"] and action["claims"]["final"] == []
        assert action["review"]["status"] == "merged" and action["live"]["ok"]
        if server is None:
            assert action["live"]["url"].endswith("index.html")
        else:
            assert action["live"]["url"].startswith("http://127.0.0.1:")
        assert (tmp_path / "demo" / "site" / "answers" / action["slug"] / "index.html").exists()
        assert "## Questions to record" in result["report"]["text"] and result["report"]["complete"]
    finally:
        if server is not None:
            server.shutdown()


def test_demo_falls_back_to_checking_the_file_without_a_port(tmp_path, monkeypatch):
    def no_port(*a, **k):
        raise OSError("no sockets here")
    monkeypatch.setattr(demo, "start_server", no_port)
    result, _, server = run_demo(tmp_path / "demo")
    assert server is None and result["status"] == "published"
    assert result["action"]["live"]["url"].endswith("index.html")


def test_demo_never_clears_a_folder_it_did_not_make(tmp_path):
    (tmp_path / "notes.txt").write_text("keep me")
    with pytest.raises(RuntimeError, match="not made by the demo"):
        run_demo(tmp_path)
    assert (tmp_path / "notes.txt").exists()


# ---- switches ----------------------------------------------------------------------------

def test_dry_run_is_the_default_and_publishes_nothing(tmp_path):
    write_answer(tmp_path / "answers")
    r, cfg = run(tmp_path, publish={"mode": "dry-run"})
    assert r["status"] == "dry-run" and r["action"]["publish"]["status"] == "dry-run"
    assert not page_file(tmp_path).exists()
    assert (tmp_path / "state" / "drafts" / (SLUG + ".html")).exists()
    assert "published" not in (tmp_path / "state" / "ledger.jsonl").read_text()


def test_review_on_and_auto_merge_off_waits_for_a_person_then_publishes(tmp_path):
    write_answer(tmp_path / "answers")
    fake, review = github()
    r, _ = run(tmp_path, gh=review, review=REVIEW)
    assert r["status"] == "in_review" and r["action"]["review"]["status"] == "waiting"
    gates = {g["name"]: g["ok"] for g in r["action"]["merge_gates"]}
    assert gates["auto-merge"] is False and all(v for k, v in gates.items() if k != "auto-merge")
    assert not page_file(tmp_path).exists()
    head = fake.pulls[1]["head"]["sha"]
    fake.route("PUT", "/pulls/1/merge", {"sha": head, "merge_method": "squash"}, {})  # a person merges
    r2, _ = run(tmp_path, gh=review, review=REVIEW)
    assert r2["action"]["kind"] == "finish" and r2["status"] == "published"
    assert page_file(tmp_path).exists()


def test_a_pull_request_edited_before_the_merge_is_not_published(tmp_path):
    write_answer(tmp_path / "answers")
    fake, review = github()
    run(tmp_path, gh=review, review=REVIEW)
    fake.push(1, "answers/%s/index.html" % SLUG, "<p>edited by hand</p>")
    fake.route("PUT", "/pulls/1/merge", {"sha": fake.pulls[1]["head"]["sha"], "merge_method": "squash"}, {})
    r, _ = run(tmp_path, gh=review, review=REVIEW)
    assert r["status"] == "held" and "differ from the approved build" in r["blockers"][0]
    assert not page_file(tmp_path).exists()


def test_a_closed_pull_request_is_not_rebuilt_until_the_recording_changes(tmp_path):
    write_answer(tmp_path / "answers")
    fake, review = github()
    run(tmp_path, gh=review, review=REVIEW)
    fake.pulls[1]["state"] = "closed"
    r, _ = run(tmp_path, gh=review, review=REVIEW)
    assert r["status"] == "closed"
    r2, _ = run(tmp_path, gh=review, review=REVIEW)
    assert r2["action"] is None and any(ln["verdict"] == "held" for ln in r2["plan"]["lines"])
    assert len(fake.pulls) == 1


def test_auto_merge_needs_publish_mode_live(tmp_path):
    write_answer(tmp_path / "answers")
    _, review = github()
    r, _ = run(tmp_path, gh=review, review={**REVIEW, "auto_merge": True}, publish={"mode": "dry-run"})
    gates = {g["name"]: g for g in r["action"]["merge_gates"]}
    assert not gates["publish mode"].get("ok") and r["status"] == "in_review"


def test_merge_state_changing_after_gates_blocks_publication_and_keeps_reporting(tmp_path):
    write_answer(tmp_path / "answers")
    fake, review = github()
    route = fake.route
    reads = 0

    def become_unstable(method, path, data, query):
        nonlocal reads
        if method == "GET" and path == "/pulls/1":
            reads += 1
            if reads == 2:
                fake.pulls[1].update(mergeable_state="unstable")
        return route(method, path, data, query)

    fake.route = become_unstable
    result, cfg = run(tmp_path, gh=review, review={**REVIEW, "auto_merge": True}, mode="force")
    assert reads == 2 and result["blockers"] and result["status"] != "published"
    assert "report" in result and not page_file(tmp_path).exists()
    assert not any(method == "PUT" and path.endswith("/merge") for method, path, _ in fake.calls)
    assert json.loads((cfg.state_dir / "last-run.json").read_text())["blockers"]


# ---- live checks, rollback and repair ----------------------------------------------------

def test_a_failed_live_check_rolls_the_publish_back(tmp_path):
    write_answer(tmp_path / "answers")
    r, _ = run(tmp_path, fetch=lambda url: Response(404, url, {}, ""))
    assert r["status"] == "rolled back" and "removed index.html" in r["action"]["rollback"]
    assert not page_file(tmp_path).exists()
    assert "live check failed" in r["blockers"][0]


def test_a_live_page_that_breaks_is_repaired_from_the_approved_build(tmp_path):
    write_answer(tmp_path / "answers")
    assert run(tmp_path)[0]["status"] == "published"
    path = page_file(tmp_path)
    path.write_text(path.read_text().replace("<title>", '<meta name="robots" content="noindex"><title>'))
    r, _ = run(tmp_path, today=TODAY + dt.timedelta(days=1))
    assert r["live_gate"][0]["ok"] is False and "noindex is set" in r["live_gate"][0]["problems"]
    assert r["action"]["kind"] == "repair" and r["status"] == "published"
    assert "noindex" not in path.read_text()


def test_git_target_publishes_by_merge_and_waits_for_the_deploy(tmp_path):
    write_answer(tmp_path / "answers")
    fake, review = github()
    sections = {"review": {**REVIEW, "auto_merge": True}, "publish": {"target": "git", "mode": "live"}}
    r, _ = run(tmp_path, gh=review, **sections)
    assert r["status"] == "not live" and r["action"]["review"]["status"] == "merged"
    path = page_file(tmp_path)  # the host deploys the merged repo
    path.parent.mkdir(parents=True)
    path.write_text(review.base_file("answers/%s/index.html" % SLUG))
    r2, _ = run(tmp_path, gh=review, **sections)
    assert r2["action"]["kind"] == "finish" and r2["status"] == "published"


# ---- planning across runs ----------------------------------------------------------------

def test_a_held_build_waits_until_the_rules_or_the_recording_change(tmp_path):
    write_answer(tmp_path / "answers", question="Who is the cheapest plumber in town?",
                 transcript="We give a written quote before any work starts, every single time.")
    r, _ = run(tmp_path)
    assert r["status"] == "held" and "claims" in r["blockers"][0]
    r2, _ = run(tmp_path)
    assert r2["action"] is None and r2["plan"]["lines"][0]["verdict"] == "held"
    rules = tmp_path / "rules.toml"
    rules.write_text('name = "no rules"\n')
    r3, _ = run(tmp_path, claims={"rules": str(rules)})
    assert r3["status"] == "published"


def test_the_daily_cap_counts_new_pages(tmp_path):
    write_answer(tmp_path / "answers")
    write_answer(tmp_path / "answers", "ra-101", "How do I fix a running toilet?",
                 transcript="A running toilet almost always has a worn flapper valve inside the tank.")
    assert run(tmp_path)[0]["status"] == "published"
    r, _ = run(tmp_path)
    assert r["action"] is None and "cap reached" in r["plan"]["note"]
    assert run(tmp_path, today=TODAY + dt.timedelta(days=1))[0]["status"] == "published"


def test_a_search_outage_is_a_blocker_and_the_run_goes_on(tmp_path):
    write_answer(tmp_path / "answers")
    r, _ = run(tmp_path, rows=ListSource(error="quota exceeded"), mode="force")
    assert any("search data unavailable" in b for b in r["blockers"])
    assert r["status"] == "published"  # an approved recording does not need search data
    assert "Search data is unavailable: RuntimeError: quota exceeded" in r["report"]["text"]


def test_a_low_ctr_page_the_engine_built_gets_a_description_from_the_transcript(tmp_path):
    write_answer(tmp_path / "answers")
    run(tmp_path)
    url = "/answers/%s/" % SLUG
    rows = daily("faucet fixed in an hour", url, 1200, 6, 3.0, END)
    r, _ = run(tmp_path, rows=rows, today=TODAY + dt.timedelta(days=1))
    assert r["action"]["kind"] == "snippet" and r["status"] == "published"
    assert r["action"]["description"]["after"] == \
        "Most faucets can be fixed in under an hour with a replacement cartridge."
    assert 'content="Most faucets can be fixed in under an hour' in page_file(tmp_path).read_text()
    r2, _ = run(tmp_path, rows=rows, today=TODAY + dt.timedelta(days=2))
    assert r2["action"] is None  # one description change per page per 28 days


# ---- the weekly report -------------------------------------------------------------------

def test_the_report_waits_for_complete_final_data(tmp_path):
    rows = daily("kitchen faucet dripping", "/", 400, 4, 15.0, dt.date(2026, 3, 6))
    r, _ = run(tmp_path, rows=rows, mode="force")
    assert "Not available yet: final Search Console data runs through 2026-03-06" in r["report"]["text"]
    assert r["report"]["complete"] is False


@pytest.mark.parametrize("checkpoint", ["{", "[]", "null"])
def test_bad_report_checkpoint_does_not_lose_completed_publication(tmp_path, checkpoint):
    write_answer(tmp_path / "answers")
    state = tmp_path / "state"
    state.mkdir()
    path = state / "report.json"
    path.write_text(checkpoint)
    result, _ = run(tmp_path, mode="auto")
    assert result["status"] == "published" and result["blockers"]
    record = json.loads((state / "pages.json").read_text())[SLUG]
    assert record["live"] and record["publication"]["status"] == "done"
    assert json.loads((state / "last-run.json").read_text())["blockers"]
    assert path.read_text() == checkpoint


@pytest.mark.parametrize("connector", ["github", "searchconsole", "shopify"])
@pytest.mark.parametrize("failure", ["invalid", "transport", "response"])
def test_connector_credentials_never_reach_errors_state_or_reports(tmp_path, monkeypatch, connector, failure):
    write_answer(tmp_path / "answers")
    secret = "synthetic-credential-marker"
    token = secret + "\n" if failure == "invalid" else secret
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append(method)
        if failure == "transport":
            raise ValueError("invalid credential: " + str(headers))
        return 401, json.dumps({"message": "invalid credential: " + token}).encode()

    monkeypatch.setattr("seo_engine.http.urllib_transport", transport)
    sections = {}
    if connector == "github":
        monkeypatch.setenv("GITHUB_TOKEN", token)
        sections["review"] = REVIEW
    elif connector == "shopify":
        monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN", token)
        monkeypatch.setenv("SHOPIFY_STORE", "synthetic-store.myshopify.com")
        sections.update(publish={"target": "shopify"}, site={"page_path": "/pages/{slug}"})
    else:
        monkeypatch.setenv("GSC_ACCESS_TOKEN", token)
        sections["search"] = {"source": "searchconsole", "site_url": "sc-domain:example.com"}
    cfg = make_cfg(tmp_path, **sections)
    deps = Deps(search=None if connector == "searchconsole" else ListSource())
    result = Engine(cfg, deps, today=TODAY).run(report="force")
    assert result["blockers"] and secret not in json.dumps(result)
    for folder in (cfg.state_dir, cfg.reports_dir):
        for path in folder.rglob("*"):
            if path.is_file():
                assert secret not in path.read_text(), path.name
    assert len(calls) == (0 if failure == "invalid" else 1)


def test_the_report_is_sent_once_a_week_when_delivery_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_ENGINE_REPORT_WEBHOOK", "https://hooks.example.com/services/test")
    sent = []

    def hook(method, url, headers, body, timeout):
        sent.append(json.loads(body))
        return 200, b"ok"
    rows = daily("kitchen faucet dripping", "/", 400, 4, 15.0, END) + \
        daily("kitchen faucet dripping", "/", 300, 3, 16.0, END - dt.timedelta(days=28))
    sections = {"report": {"send": "webhook", "weekday": "thursday", "payload_key": "content"}}
    r, _ = run(tmp_path, rows=rows, webhook=hook, mode="auto", **sections)
    assert r["report"]["sent"] and list(sent[0]) == ["content"]
    assert "| Clicks |" in sent[0]["content"]
    r2, _ = run(tmp_path, rows=rows, webhook=hook, mode="auto", **sections)
    assert "report" not in r2 and len(sent) == 1


def test_the_webhook_needs_https_and_never_leaks_its_url(monkeypatch):
    monkeypatch.delenv("SEO_ENGINE_REPORT_WEBHOOK", raising=False)
    with pytest.raises(RuntimeError, match="https"):
        send_webhook("hi", url="http://hooks.example.com/x")
    with pytest.raises(RuntimeError) as e:
        send_webhook("hi", url="https://hooks.example.com/secret-path", transport=lambda *a: (500, b""))
    assert "secret-path" not in str(e.value) and "500" in str(e.value)


def test_a_merge_by_a_person_does_not_skip_the_approval_check(tmp_path):
    write_answer(tmp_path / "answers")
    fake, review = github()
    run(tmp_path, gh=review, review=REVIEW)
    fake.route("PUT", "/pulls/1/merge", {"sha": fake.pulls[1]["head"]["sha"], "merge_method": "squash"}, {})
    meta = json.loads((tmp_path / "answers" / "ra-100.json").read_text())
    meta["approval"]["status"] = "withdrawn"
    (tmp_path / "answers" / "ra-100.json").write_text(json.dumps(meta))
    r, _ = run(tmp_path, gh=review, review=REVIEW)
    assert r["status"] == "held" and "before publishing" in r["blockers"][0]
    assert not page_file(tmp_path).exists()


def test_a_snippet_finds_its_page_when_search_console_drops_the_trailing_slash(tmp_path):
    write_answer(tmp_path / "answers")
    run(tmp_path)
    rows = daily("faucet fixed in an hour", "/answers/%s" % SLUG, 1200, 6, 3.0, END)  # no trailing slash
    r, _ = run(tmp_path, rows=rows, today=TODAY + dt.timedelta(days=1))
    assert r["action"]["kind"] == "snippet" and r["action"]["slug"] == SLUG and r["status"] == "published"
