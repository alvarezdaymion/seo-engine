import datetime as dt
import json

import pytest
from helpers import TODAY, ListSource, daily, make_cfg, write_answer
from test_draft import FakeClient
from test_engine import REVIEW, SLUG, github, page_file, run
from test_shopify import FakeShopify, make, make_page, writes

from seo_engine.answers import approve, load_answer
from seo_engine.draft import grounding_problems
from seo_engine.engine import Deps, Engine
from seo_engine.page import build_page
from seo_engine.publish.static import StaticPublisher
from seo_engine.report import build as report_build
from seo_engine.review import GitHubReview


@pytest.mark.parametrize("fmt,pattern", [("html", "{slug}.html"), ("markdown", "{slug}.md")])
def test_static_collision_is_refused(tmp_path, fmt, pattern):
    page = make_page(tmp_path / "answers")
    site = tmp_path / "site"
    site.mkdir()
    dest = site / pattern.format(slug=page.slug)
    dest.write_text("Maintained by a person.")
    with pytest.raises(PermissionError):
        StaticPublisher(site, pattern, fmt).publish(page)
    assert dest.read_text() == "Maintained by a person."


def test_git_collision_is_refused_before_any_write(tmp_path):
    write_answer(tmp_path / "answers")
    fake, review = github()
    fake.trees["t0"]["answers/%s/index.html" % SLUG] = "Existing site content."
    result, _ = run(tmp_path, gh=review, review={**REVIEW, "auto_merge": True}, publish={"target": "git"})
    assert result["blockers"] and fake.refs["main"] == "c0"
    assert all(method == "GET" for method, _, _ in fake.calls)


def test_shopify_collision_is_refused_without_mutation(tmp_path):
    page = make_page(tmp_path)
    fake = FakeShopify([{"id": 123, "handle": page.slug, "title": "Existing page",
                         "body_html": "Original content", "published_at": "2026-01-01"}])
    with pytest.raises(PermissionError):
        make(fake).publish(page)
    assert writes(fake) == []


@pytest.mark.parametrize("replacement", [
    "The valve costs ten dollars, not two dollars.",
    "The valve costs two dollars.",
    "The valve costs two dollars, not ten.",
])
def test_grounding_keeps_sentence_meaning_and_qualifiers(tmp_path, replacement):
    source = "The valve costs two dollars, not ten dollars."
    assert grounding_problems(replacement, [source])
    answer = write_answer(tmp_path, transcript=source)
    with pytest.raises(ValueError):
        build_page(answer, answer_sentences=[replacement], slug="valve", canonical="https://www.example.com/valve",
                   site_name="Example", published_on=TODAY.isoformat(), disclosures=[])


@pytest.mark.parametrize("change", [
    {"cut": ["Good question."]}, {"question": "What makes a faucet drip?"},
    {"speaker": {"name": "Pat Placeholder", "role": "Technician"}},
    {"video_url": "https://videos.example.com/changed.mp4"},
    {"thumbnail_url": "https://videos.example.com/changed.jpg"}, {"recorded_on": "2026-01-02"},
    {"sources": [{"url": "https://www.example.org/source", "title": "Source", "supports": "faucet"}]},
])
def test_approval_covers_publication_metadata(tmp_path, change):
    answer = write_answer(tmp_path)
    path = tmp_path / "ra-100.json"
    meta = json.loads(path.read_text())
    meta.update(change)
    path.write_text(json.dumps(meta))
    assert answer.approved and not load_answer(path).approved
    assert approve(path, "owner", TODAY).approved


def test_reapproval_does_not_authorize_an_older_pending_build(tmp_path):
    write_answer(tmp_path / "answers")
    _, review = github()
    run(tmp_path, gh=review, review=REVIEW)
    path = tmp_path / "answers" / "ra-100.json"
    meta = json.loads(path.read_text())
    meta["cut"] = ["Hard water wears those parts faster than soft water does."]
    path.write_text(json.dumps(meta))
    approve(path, "owner", TODAY)
    result, _ = run(tmp_path, gh=review, review={**REVIEW, "auto_merge": True})
    assert result["status"] != "published" and not page_file(tmp_path).exists()
    assert not any(m == "PUT" for m, _, _ in review.http.transport.calls)


@pytest.mark.parametrize("change", ["approval", "rules"])
def test_content_is_reloaded_after_github_checks(tmp_path, change):
    write_answer(tmp_path / "answers")
    fake, review = github()
    original = review.gates
    rules = tmp_path / "rules.toml"
    rules.write_text('name = "Example"\n')

    def gates(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "approval":
            path = tmp_path / "answers" / "ra-100.json"
            meta = json.loads(path.read_text())
            meta["approval"]["status"] = "withdrawn"
            path.write_text(json.dumps(meta))
        else:
            rules.write_text('[[banned]]\nid = "faucet"\nphrases = ["faucet"]\n')
        return result

    review.gates = gates
    result, _ = run(tmp_path, gh=review, review={**REVIEW, "auto_merge": True}, claims={"rules": str(rules)})
    assert result["status"] != "published" and fake.refs["main"] == "c0"
    assert not page_file(tmp_path).exists()


def test_live_checker_exception_causes_rollback_and_report(tmp_path, monkeypatch):
    write_answer(tmp_path / "answers")

    def broken(*args, **kwargs):
        raise ValueError("synthetic parser failure")

    monkeypatch.setattr("seo_engine.engine.check_until", broken)
    result, _ = run(tmp_path, mode="force")
    assert result["status"] == "rolled back" and not page_file(tmp_path).exists()
    assert result["action"]["rollback"] and "report" in result


def test_partial_shopify_update_restores_or_hides_before_returning(tmp_path):
    fake = FakeShopify()
    shop = make(fake)
    old = make_page(tmp_path / "old")
    shop.publish(old)
    previous = dict(fake.pages[100])
    new = make_page(tmp_path / "new", description_override="Updated description.")
    new.answer = new.answer[:1]

    def fail_once(method, url, headers, body, timeout):
        result = fake(method, url, headers, body, timeout)
        if method == "PUT" and "/metafields/" in url and not getattr(fail_once, "failed", False):
            fail_once.failed = True
            raise TimeoutError("synthetic lost response")
        return result

    shop.http.transport = fail_once
    with pytest.raises(RuntimeError):
        shop.publish(new)
    assert fake.pages[100]["body_html"] == previous["body_html"] or not fake.pages[100]["published_at"]
    if fake.pages[100]["published_at"]:
        assert fake.metafields[100][1]["value"] == old.description


def test_uncertain_shopify_creation_is_not_repeated(tmp_path):
    write_answer(tmp_path / "answers")
    fake = FakeShopify()
    shop = make(fake)

    def lost_response(method, url, headers, body, timeout):
        result = fake(method, url, headers, body, timeout)
        if method == "POST":
            raise TimeoutError("synthetic accepted creation")
        return result

    shop.http.transport = lost_response
    cfg = make_cfg(tmp_path, publish={"target": "shopify"}, site={"page_path": "/pages/{slug}"})
    for _ in range(2):
        result = Engine(cfg, Deps(search=ListSource(), publisher=shop), today=TODAY).run(report="off")
        assert result["blockers"]
    assert len(fake.pages) == 1 and not next(iter(fake.pages.values()))["published_at"]
    assert len([c for c in fake.calls if c[0] == "POST"]) == 1
    record = json.loads((tmp_path / "state" / "pages.json").read_text())[SLUG]
    assert record["publication"]["undo"]


def test_uncertain_webhook_is_held_even_for_forced_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_ENGINE_REPORT_WEBHOOK", "https://hooks.example.com/test")
    deliveries = []

    def hook(*args):
        deliveries.append(1)
        raise TimeoutError("synthetic accepted delivery")

    for _ in range(2):
        result, _ = run(tmp_path, webhook=hook, mode="force", report={"send": "webhook"})
        assert result["blockers"]
    assert deliveries == [1]


def test_lost_pr_response_is_reconciled_without_another_write(tmp_path):
    write_answer(tmp_path / "answers")
    fake, review = github()

    def accepted(method, url, headers, body, timeout):
        result = fake(method, url, headers, body, timeout)
        if method == "POST" and url.endswith("/pulls"):
            raise TimeoutError("synthetic accepted PR")
        return result

    review.http.transport = accepted
    first, _ = run(tmp_path, gh=review, review=REVIEW)
    assert first["blockers"] and len(fake.pulls) == 1
    fake.calls.clear()
    review = GitHubReview("example/site", token="test-token-not-real", transport=fake, sleep=lambda _: None)
    second, _ = run(tmp_path, gh=review, review=REVIEW)
    assert second["status"] == "in_review" and second["action"]["review"]["number"] == 1
    assert all(m == "GET" for m, _, _ in fake.calls)


def test_absent_reporting_weeks_are_unavailable(tmp_path):
    rows = daily("synthetic question", "/", 10, 1, 5, TODAY, days=1)
    text, meta = report_build(make_cfg(tmp_path), rows, TODAY, TODAY, {}, [], None, [], [])
    assert not meta["complete"] and "clicks" not in meta and "unavailable" in text.lower()


def test_shopify_preview_needs_no_publishing_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("SHOPIFY_STORE", raising=False)
    monkeypatch.delenv("SHOPIFY_ADMIN_TOKEN", raising=False)
    write_answer(tmp_path / "answers")
    result, _ = run(tmp_path, publish={"target": "shopify", "mode": "dry-run"},
                    site={"page_path": "/pages/{slug}"})
    assert result["status"] == "dry-run" and not result["blockers"]


def test_snippet_preview_preserves_the_next_live_opportunity(tmp_path):
    write_answer(tmp_path / "answers")
    run(tmp_path)
    rows = daily("faucet fixed in an hour", "/answers/%s/" % SLUG, 1200, 6, 3, TODAY - dt.timedelta(days=2))
    preview, _ = run(tmp_path, rows=rows, today=TODAY + dt.timedelta(days=1), publish={"mode": "dry-run"})
    assert preview["status"] == "dry-run" and preview["action"]["kind"] == "snippet"
    published, _ = run(tmp_path, rows=rows, today=TODAY + dt.timedelta(days=2))
    assert published["status"] == "published" and published["action"]["kind"] == "snippet"


def test_removed_long_claim_cannot_remain_in_a_truncated_description(tmp_path):
    claim = ("This replacement reduces water use by 50 percent in homes with worn washers, "
             "based on measurements from a wide range of household layouts, plumbing systems, "
             "and installation conditions across the sample.")
    write_answer(tmp_path / "answers", transcript=claim + " We inspect the cartridge before starting work.")
    result, _ = run(tmp_path)
    assert result["status"] == "published"
    assert "50 percent" not in page_file(tmp_path).read_text()


def test_report_fetch_covers_both_weeks_with_a_short_ranking_window(tmp_path):
    end = dt.date(2026, 3, 8)
    rows = daily("synthetic question", "/", 140, 14, 5, end, days=14)
    result, _ = run(tmp_path, rows=rows, search={"days": 1}, mode="force")
    assert result["report"]["complete"] and result["report"]["clicks"] == [7, 7]


def test_overlapping_run_is_held_before_any_connector_call(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    lock = state / "run.lock"
    lock.write_text("pending run")
    write_answer(tmp_path / "answers")
    fake, review = github()
    result, _ = run(tmp_path, gh=review, review=REVIEW)
    assert result["blockers"] and fake.calls == [] and lock.read_text() == "pending run"


def test_repeated_preview_does_not_repeat_an_uncertain_model_charge(tmp_path):
    write_answer(tmp_path / "answers")
    client = FakeClient()

    def lost(*args, **kwargs):
        client.calls.append(kwargs)
        raise TimeoutError("synthetic lost model response")

    client.messages.create = lost
    cfg = make_cfg(tmp_path, draft={"mode": "model", "fallbacks": False}, publish={"mode": "dry-run"})
    for _ in range(2):
        result = Engine(cfg, Deps(search=ListSource(), llm=client), today=TODAY).run(report="off")
        assert result["status"] == "dry-run"
    assert len(client.calls) == 1


def test_shopify_reload_after_remote_lookup_prevents_publication(tmp_path):
    answer = write_answer(tmp_path / "answers")
    fake = FakeShopify()
    shop = make(fake)

    def withdraw(method, url, headers, body, timeout):
        response = fake(method, url, headers, body, timeout)
        if method == "GET":
            meta = json.loads(answer.path.read_text())
            meta["approval"]["status"] = "withdrawn"
            answer.path.write_text(json.dumps(meta))
        return response

    shop.http.transport = withdraw
    cfg = make_cfg(tmp_path, publish={"target": "shopify"}, site={"page_path": "/pages/{slug}"})
    result = Engine(cfg, Deps(search=ListSource(), publisher=shop), today=TODAY).run(report="off")
    assert result["blockers"] and writes(fake) == []


def test_invalid_search_dates_are_unavailable_and_do_not_abort_reporting(tmp_path):
    from seo_engine.search import Row

    class InvalidSource(ListSource):
        def fetch(self, start, end):
            return [Row("invalid", "synthetic question", "https://www.example.com/", 1, 10, 5)]

    result, _ = run(tmp_path, rows=InvalidSource(), mode="force")
    assert result["blockers"] and not result["report"]["complete"]


def test_initial_live_check_exception_is_isolated_and_reporting_continues(tmp_path, monkeypatch):
    write_answer(tmp_path / "answers")
    run(tmp_path)

    def broken(*args, **kwargs):
        raise ValueError("synthetic parser error")

    monkeypatch.setattr("seo_engine.engine.live_check", broken)
    result, _ = run(tmp_path, mode="force")
    assert not result["live_gate"][0]["ok"] and "report" in result


def test_corrupt_managed_body_is_repaired_from_its_saved_build(tmp_path):
    write_answer(tmp_path / "answers")
    assert run(tmp_path)[0]["status"] == "published"
    path = page_file(tmp_path)
    original = path.read_text()
    path.write_text(original.replace("usually means", "never means"))
    result, _ = run(tmp_path, mode="force")
    assert result["action"]["kind"] == "repair" and result["status"] == "published"
    assert path.read_text() == original and not result["blockers"]


def test_managed_ownership_does_not_transfer_to_a_different_site_folder(tmp_path):
    write_answer(tmp_path / "answers")
    run(tmp_path)
    path = tmp_path / "other-site" / "answers" / SLUG / "index.html"
    path.parent.mkdir(parents=True)
    path.write_text("Unmanaged content.")
    result, _ = run(tmp_path, static={"dir": "other-site"})
    assert result["blockers"] and path.read_text() == "Unmanaged content."


def test_managed_ownership_does_not_follow_a_retargeted_symlink(tmp_path):
    write_answer(tmp_path / "answers")
    run(tmp_path)
    manual = tmp_path / "site" / "manual.html"
    manual.write_text("Unmanaged content.")
    path = page_file(tmp_path)
    path.unlink()
    path.symlink_to(manual)
    result, _ = run(tmp_path)
    assert result["blockers"] and manual.read_text() == "Unmanaged content."


@pytest.mark.parametrize("change", ["role", "queries"])
def test_closing_an_old_build_does_not_reject_a_new_approved_revision(tmp_path, change):
    answer = write_answer(tmp_path / "answers")
    fake, review = github()
    assert run(tmp_path, gh=review, review=REVIEW)[0]["status"] == "in_review"
    meta = json.loads(answer.path.read_text())
    if change == "role":
        meta["speaker"]["role"] = "Technician"
    else:
        meta["target_queries"] = ["synthetic replacement question"]
    answer.path.write_text(json.dumps(meta))
    approve(answer.path, "owner", TODAY)
    fake.pulls[1]["state"] = "closed"
    assert run(tmp_path, gh=review, review=REVIEW)[0]["status"] == "closed"
    result, _ = run(tmp_path, gh=review, review=REVIEW)
    assert result["status"] == "in_review" and len(fake.pulls) == 2
    assert result["action"]["kind"] == "build"


def test_closing_a_legacy_pending_build_still_holds_the_same_revision(tmp_path):
    write_answer(tmp_path / "answers")
    fake, review = github()
    run(tmp_path, gh=review, review=REVIEW)
    path = tmp_path / "state" / "pages.json"
    records = json.loads(path.read_text())
    records[SLUG].pop("pending_key", None)
    path.write_text(json.dumps(records))
    fake.pulls[1]["state"] = "closed"
    assert run(tmp_path, gh=review, review=REVIEW)[0]["status"] == "closed"
    result, _ = run(tmp_path, gh=review, review=REVIEW)
    assert result["action"] is None and len(fake.pulls) == 1


@pytest.mark.parametrize("damage", ["truncated", "missing"])
def test_bad_saved_build_does_not_stop_other_live_checks_or_reporting(tmp_path, damage):
    write_answer(tmp_path / "answers")
    write_answer(tmp_path / "answers", "ra-200", question="Why does a tap squeak?",
                 transcript="A worn seal can make the tap squeak when the handle turns.")
    assert run(tmp_path)[0]["status"] == "published"
    assert run(tmp_path, today=TODAY + dt.timedelta(days=1))[0]["status"] == "published"
    build = tmp_path / "state" / "pages" / (SLUG + ".json")
    if damage == "missing":
        build.unlink()
    else:
        build.write_text("{")
    result, _ = run(tmp_path, today=TODAY + dt.timedelta(days=2), mode="force")
    assert result["blockers"] and "report" in result
    assert len(result["live_gate"]) == 2
    assert any(check["ok"] for check in result["live_gate"])
    saved = json.loads((tmp_path / "state" / "last-run.json").read_text())
    assert saved["blockers"] and saved["today"] == (TODAY + dt.timedelta(days=2)).isoformat()
    assert not build.exists() if damage == "missing" else build.read_text() == "{"
