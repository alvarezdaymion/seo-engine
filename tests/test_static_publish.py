import copy
import datetime as dt
import json

import pytest
from helpers import RULES, TODAY, daily, make_cfg, write_answer
from test_engine import SLUG, page_file, run
from test_livecheck import make_page

from seo_engine.answers import approve
from seo_engine.livecheck import Response, file_fetch
from seo_engine.page import render_html
from seo_engine.publish.static import StaticPublisher
from seo_engine.state import State


def test_static_temporary_symlink_cannot_overwrite_an_unmanaged_file(tmp_path):
    publisher = StaticPublisher(tmp_path / "site", "{slug}.html")
    publisher.root.mkdir()
    manual = tmp_path / "manual.html"
    manual.write_text("Unmanaged content.")
    destination = publisher.root / "faucet.html"
    trap = destination.with_suffix(".html.tmp")
    trap.symlink_to(manual)
    published = publisher.publish(make_page(tmp_path / "answers"))
    assert published.action == "created" and destination.is_file() and not destination.is_symlink()
    assert manual.read_text() == "Unmanaged content." and trap.is_symlink()


@pytest.mark.parametrize("existing", [False, True])
def test_static_rollback_refuses_a_substituted_destination(tmp_path, existing):
    publisher = StaticPublisher(tmp_path / "site", "{slug}.html")
    page = make_page(tmp_path / "answers")
    if existing:
        publisher.publish(page)
    updated = copy.deepcopy(page)
    updated.description = "Changed description."
    published = publisher.publish(updated)
    destination = publisher.root / "faucet.html"
    manual = tmp_path / "manual.html"
    manual.write_text("Unmanaged content.")
    destination.unlink()
    destination.symlink_to(manual)
    publisher.before_restore = lambda undo: None
    with pytest.raises(PermissionError):
        publisher.rollback(published)
    assert manual.read_text() == "Unmanaged content." and destination.is_symlink()


def test_static_rollback_refuses_a_substituted_parent_directory(tmp_path):
    publisher = StaticPublisher(tmp_path / "site", "answers/{slug}/index.html")
    page = make_page(tmp_path / "answers")
    publisher.publish(page)
    page.description = "Changed description."
    published = publisher.publish(page)
    parent = publisher.root / "answers" / "faucet"
    outside = tmp_path / "manual"
    outside.mkdir()
    manual = outside / "index.html"
    manual.write_text("Unmanaged content.")
    parent.rename(parent.with_name("old-faucet"))
    parent.symlink_to(outside, target_is_directory=True)
    publisher.before_restore = lambda undo: None
    with pytest.raises(PermissionError):
        publisher.rollback(published)
    assert manual.read_text() == "Unmanaged content."


def test_static_write_refuses_a_parent_retargeted_during_its_gate(tmp_path):
    publisher = StaticPublisher(tmp_path / "site", "answers/{slug}/index.html")
    page = make_page(tmp_path / "answers")
    publisher.publish(page)
    parent = publisher.root / "answers" / "faucet"
    outside = tmp_path / "manual"
    outside.mkdir()
    manual = outside / "index.html"
    manual.write_text("Unmanaged content.")

    def retarget():
        parent.rename(parent.with_name("old-faucet"))
        parent.symlink_to(outside, target_is_directory=True)

    publisher.before_write = retarget
    page.description = "Changed description."
    with pytest.raises(PermissionError):
        publisher.publish(page)
    assert manual.read_text() == "Unmanaged content."


def test_successful_fetch_cannot_adopt_a_retargeted_file(tmp_path):
    write_answer(tmp_path / "answers")
    manual = tmp_path / "site" / "manual.html"
    manual.parent.mkdir()
    manual.write_text("Unmanaged content.")
    destination = page_file(tmp_path)

    def retarget_after_fetch(url):
        response = file_fetch(url)
        destination.unlink()
        destination.symlink_to(manual)
        return response

    result, cfg = run(tmp_path, fetch=retarget_after_fetch)
    assert result["blockers"] and result["status"] != "published"
    record = json.loads((cfg.state_dir / "pages.json").read_text())[SLUG]
    assert str(manual) not in (record.get("ownership") or {}).get("files", {}).values()
    assert record["publication"]["status"] == "pending"
    run(tmp_path)
    assert manual.read_text() == "Unmanaged content."


def test_missing_symlink_target_does_not_acquire_managed_ownership(tmp_path):
    write_answer(tmp_path / "answers")
    run(tmp_path)
    destination = page_file(tmp_path)
    missing = tmp_path / "site" / "other.html"
    destination.unlink()
    destination.symlink_to(missing)
    result, _ = run(tmp_path)
    assert result["blockers"] and not missing.exists()


@pytest.mark.parametrize("change", ["withdrawn", "rules", "revision", "missing", None])
@pytest.mark.parametrize("fmt", ["html", "markdown"])
def test_static_restoration_requires_the_previous_builds_current_gates(tmp_path, change, fmt):
    answer = write_answer(tmp_path / "answers")
    rules = tmp_path / "rules.toml"
    rules.write_text(RULES.read_text())
    sections = {"claims": {"rules": str(rules)}}
    if fmt == "markdown":
        sections["static"] = {"format": "markdown", "file": "answers/{slug}/index.md"}
    cfg = make_cfg(tmp_path, **sections)

    def preview(url):
        state = State(cfg.state_dir)
        page = state.page(SLUG, pending=True) or state.page(SLUG)
        return Response(200, url, {}, render_html(page))

    fetch = preview if fmt == "markdown" else file_fetch
    first, _ = run(tmp_path, fetch=fetch, **sections)
    assert first["status"] == "published"
    destination = page_file(tmp_path).with_suffix(".md" if fmt == "markdown" else ".html")
    old = destination.read_text()
    calls = 0

    def fail_after_update(url):
        nonlocal calls
        calls += 1
        if calls == 1:
            return fetch(url)
        if change == "rules":
            rules.write_text('[[banned]]\nid = "faucet"\nphrases = ["faucet"]\n')
        elif change == "missing":
            (tmp_path / "state" / "pages" / (SLUG + ".json")).unlink()
        elif change:
            metadata = json.loads(answer.path.read_text())
            if change == "withdrawn":
                metadata["approval"]["status"] = "withdrawn"
            else:
                metadata["speaker"]["role"] = "Technician"
            answer.path.write_text(json.dumps(metadata))
            if change == "revision":
                approve(answer.path, "owner", TODAY)
        return Response(503, url, {}, "")

    rows = daily("faucet fixed in an hour", "/answers/" + SLUG + "/", 1200, 0, 3, TODAY)
    result, cfg = run(tmp_path, rows=rows, fetch=fail_after_update, today=TODAY + dt.timedelta(days=1),
                      mode="force", **sections)
    assert calls == 2 and result["action"]["kind"] == "snippet" and result["blockers"]
    if change:
        assert not destination.exists()
        record = json.loads((cfg.state_dir / "pages.json").read_text())[SLUG]
        assert not record["live"]
    else:
        assert destination.read_text() == old and result["status"] == "rolled back"


def test_static_rollback_without_a_release_gate_unpublishes_the_owned_file(tmp_path):
    publisher = StaticPublisher(tmp_path / "site", "{slug}.html")
    page = make_page(tmp_path / "answers")
    publisher.publish(page)
    page.description = "Changed description."
    published = publisher.publish(page)
    publisher.rollback(published)
    assert not (publisher.root / "faucet.html").exists()
