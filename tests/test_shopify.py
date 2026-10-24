import json
import re
import urllib.parse

import pytest
from helpers import write_answer

from seo_engine.http import HttpError
from seo_engine.page import build_page, kept_transcript, render_body
from seo_engine.publish.shopify import ShopifyPublisher

TOKEN = "shpat-test-not-real"
BASE = "/admin/api/2026-07"


class FakeShopify:
    def __init__(self, pages=(), metafields=None):
        self.pages = {p["id"]: dict(p) for p in pages}
        self.metafields = metafields or {}
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        assert headers["X-Shopify-Access-Token"] == TOKEN
        parts = urllib.parse.urlsplit(url)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        data = json.loads(body) if body else None
        self.calls.append((method, path, data))
        status, out = self.route(method, path, query, data)
        return status, json.dumps(out).encode()

    def route(self, method, path, query, data):
        if method == "GET" and path == BASE + "/pages.json":
            return 200, {"pages": [p for p in self.pages.values() if p["handle"] == query["handle"][0]]}
        m = re.fullmatch(BASE + r"/pages/(\d+)/metafields\.json", path)
        if method == "GET" and m:
            return 200, {"metafields": self.metafields.get(int(m.group(1)), [])}
        if method == "POST" and path == BASE + "/pages.json":
            page = dict(data["page"])
            fields = page.pop("metafields")
            pid = 100 + len(self.pages)
            page.update(id=pid, published_at="2026-03-12T10:00:00Z" if page.pop("published") else None)
            self.pages[pid] = page
            self.metafields[pid] = [{"id": 900 + i, **f} for i, f in enumerate(fields)]
            return 201, {"page": page}
        m = re.fullmatch(BASE + r"/pages/(\d+)\.json", path)
        if method == "PUT" and m:
            page, change = self.pages[int(m.group(1))], dict(data["page"])
            if "published" in change:
                page["published_at"] = "2026-03-12T10:00:00Z" if change.pop("published") else None
            change.pop("metafields", None)
            page.update(change)
            return 200, {"page": page}
        m = re.fullmatch(BASE + r"/metafields/(\d+)\.json", path)
        if method == "PUT" and m:
            for fields in self.metafields.values():
                for f in fields:
                    if f["id"] == int(m.group(1)):
                        f["value"] = data["metafield"]["value"]
            return 200, {"metafield": data["metafield"]}
        return 404, {"errors": "Not Found"}


def make(fake):
    return ShopifyPublisher(store="test-store.myshopify.com", token=TOKEN, transport=fake)


def make_page(tmp_path, description_override=None):
    answer = write_answer(tmp_path)
    kept, _ = kept_transcript(answer)
    page = build_page(answer, answer_sentences=kept[0][1:3], slug="faucet", canonical="https://www.example.com/pages/faucet",
                      site_name="Test Co", published_on="2026-03-12", disclosures=[])
    if description_override:
        page.description = description_override
    return page


def writes(fake):
    return [(m, p, d) for m, p, d in fake.calls if m != "GET"]


def test_a_new_page_is_created_hidden_then_shown(tmp_path):
    fake, page = FakeShopify(), make_page(tmp_path)
    result = make(fake).publish(page)
    assert result.action == "created"
    (m1, p1, d1), (m2, p2, d2) = writes(fake)
    assert (m1, p1) == ("POST", BASE + "/pages.json") and d1["page"]["published"] is False
    assert d1["page"]["handle"] == "faucet" and d1["page"]["body_html"] == render_body(page)
    assert {f["key"]: f["value"] for f in d1["page"]["metafields"]} == {
        "title_tag": page.question, "description_tag": page.description}
    assert (m2, p2, d2) == ("PUT", BASE + "/pages/100.json", {"page": {"id": 100, "published": True}})


def test_an_unchanged_page_writes_nothing(tmp_path):
    fake, page = FakeShopify(), make_page(tmp_path)
    shop = make(fake)
    shop.publish(page)
    fake.calls.clear()
    assert shop.publish(page).action == "unchanged" and writes(fake) == []


def test_an_update_and_its_rollback(tmp_path):
    fake = FakeShopify()
    shop = make(fake)
    shop.publish(make_page(tmp_path / "a"))
    updated = make_page(tmp_path / "b", description_override="Most faucets can be fixed in under an hour.")
    fake.calls.clear()
    result = shop.publish(updated)
    assert result.action == "updated"
    assert [(m, p) for m, p, _ in writes(fake)] == [("PUT", BASE + "/pages/100.json"),
                                                    ("PUT", BASE + "/metafields/901.json")]
    assert fake.metafields[100][1]["value"] == "Most faucets can be fixed in under an hour."
    assert shop.rollback(result).startswith("restored")
    assert fake.metafields[100][1]["value"] == make_page(tmp_path / "c").description


def test_rolling_back_a_new_page_hides_it_and_never_deletes(tmp_path):
    fake = FakeShopify()
    shop = make(fake)
    result = shop.publish(make_page(tmp_path))
    assert shop.rollback(result) == "hid the new page"
    assert fake.pages[100]["published_at"] is None
    assert not [c for c in fake.calls if c[0] == "DELETE"]


@pytest.mark.parametrize("method,path,body", [
    ("DELETE", "/pages/100.json", {"page": {}}),
    ("PUT", "/themes/1.json", {"theme": {"role": "main"}}),
    ("POST", "/redirects.json", {"redirect": {"path": "/a", "target": "/b"}}),
    ("PUT", "/products/5.json", {"page": {"title": "x"}}),
    ("PUT", "/metafields/901.json", {"metafield": {"id": 901, "value": "x", "namespace": "custom"}}),
    ("PUT", "/pages/100.json", {"page": {"id": 100}, "extra": True}),
])
def test_any_other_write_is_refused_before_it_is_sent(method, path, body):
    fake = FakeShopify()
    shop = make(fake)
    with pytest.raises(PermissionError):
        shop._write(method, shop.base + path, body)
    assert fake.calls == []


def test_store_must_be_a_myshopify_domain_and_credentials_are_required(monkeypatch):
    with pytest.raises(RuntimeError, match="myshopify"):
        ShopifyPublisher(store="evil.example.com", token=TOKEN)
    monkeypatch.delenv("SHOPIFY_STORE", raising=False)
    monkeypatch.delenv("SHOPIFY_ADMIN_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="SHOPIFY_STORE"):
        ShopifyPublisher()


def test_errors_never_include_the_token(tmp_path):
    shop = ShopifyPublisher(store="test-store.myshopify.com", token=TOKEN,
                            transport=lambda *a: (401, b'{"errors": "Invalid API key or access token"}'))
    with pytest.raises(HttpError) as e:
        shop.publish(make_page(tmp_path))
    assert "401" in str(e.value) and TOKEN not in str(e.value)


def test_review_files_hold_the_exact_body_and_seo_fields(tmp_path):
    page = make_page(tmp_path)
    files = make(FakeShopify()).files(page)
    assert files["pages/faucet.html"] == render_body(page)
    meta = json.loads(files["pages/faucet.json"])
    assert meta["seo_description"] == page.description and meta["recording"] == "ra-100"
