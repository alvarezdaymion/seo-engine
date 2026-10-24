import json

import pytest
from helpers import RULES, TODAY, ListSource, daily, make_cfg, write_answer
from test_engine import SLUG
from test_shopify import FakeShopify, make, make_page

from seo_engine.answers import approve
from seo_engine.claims import load_rules
from seo_engine.engine import Deps, Engine
from seo_engine.livecheck import Response
from seo_engine.page import build_page, kept_transcript, render_body, render_html
from seo_engine.state import State


def previous_shopify_build(root):
    answer = write_answer(root / "answers", transcript=(
        "A worn cartridge can let water through a closed faucet. "
        "A replacement cartridge can stop that drip. "
        "Turn off the supply before removing the handle."))
    rules = root / "rules.toml"
    rules.write_text(RULES.read_text())
    cfg = make_cfg(root, publish={"target": "shopify"}, site={"page_path": "/pages/{slug}"},
                   claims={"rules": str(rules)}, livecheck={"fetch_base": ""})
    page = build_page(answer, answer_sentences=kept_transcript(answer)[0][0][:1], slug=SLUG,
                      canonical=cfg.page_url(SLUG), site_name=cfg["site"]["name"], published_on=TODAY.isoformat(),
                      disclosures=[text for _, text in load_rules(rules).disclosures])
    fake = FakeShopify()
    publisher = make(fake)
    publisher.publish(page)
    state = State(cfg.state_dir)
    state.store_page(page)
    state.record(SLUG).update(live=True, status="published", url=page.canonical, recording_id=answer.id)
    state.save()
    fake.calls.clear()
    rows = daily("replacement cartridge stop drip", "/pages/" + SLUG, 1200, 0, 3, TODAY)
    deps = Deps(search=ListSource(rows), publisher=publisher, sleep=lambda _: None,
                fetch=lambda url: Response(200, url, {}, render_html(page)))
    return cfg, deps, fake, answer, page


@pytest.mark.parametrize("change", ["withdrawn", "revision", "rules", "missing"])
def test_shopify_recovery_cannot_show_an_unapproved_previous_build(tmp_path, change):
    cfg, deps, fake, answer, _ = previous_shopify_build(tmp_path)
    changed = False
    failed = False

    def change_after_hiding(method, url, headers, body, timeout):
        nonlocal changed, failed
        if change == "missing" and changed and not failed and method == "PUT" and "/metafields/" in url:
            failed = True
            raise TimeoutError("synthetic failed update")
        response = fake(method, url, headers, body, timeout)
        data = json.loads(body) if body else {}
        if method == "PUT" and data.get("page", {}).get("published") is False and not changed:
            changed = True
            if change == "rules":
                (tmp_path / "rules.toml").write_text('[[banned]]\nid = "cartridge"\nphrases = ["cartridge"]\n')
            elif change == "missing":
                (cfg.state_dir / "pages" / (SLUG + ".json")).unlink()
            else:
                meta = json.loads(answer.path.read_text())
                if change == "withdrawn":
                    meta["approval"]["status"] = "withdrawn"
                else:
                    meta["speaker"]["role"] = "Technician"
                answer.path.write_text(json.dumps(meta))
                if change == "revision":
                    approve(answer.path, "owner", TODAY)
        return response

    deps.publisher.http.transport = change_after_hiding
    result = Engine(cfg, deps, today=TODAY).run(report="off")
    assert changed and result["blockers"]
    assert fake.pages[100]["published_at"] is None
    assert not any(m == "PUT" and (data.get("page") or {}).get("published") is True
                   for m, _, data in fake.calls)


def test_shopify_recovery_can_show_the_verified_approved_previous_build(tmp_path):
    cfg, deps, fake, _, page = previous_shopify_build(tmp_path)
    result = Engine(cfg, deps, today=TODAY).run(report="off")
    assert result["status"] == "rolled back"
    assert fake.pages[100]["published_at"] and fake.pages[100]["body_html"] == render_body(page)


def test_shopify_rollback_without_a_previous_build_gate_keeps_it_hidden(tmp_path):
    fake = FakeShopify()
    publisher = make(fake)
    publisher.publish(make_page(tmp_path / "old"))
    updated = publisher.publish(make_page(tmp_path / "new", description_override="Changed description."))
    publisher.rollback(updated)
    assert fake.pages[100]["published_at"] is None
