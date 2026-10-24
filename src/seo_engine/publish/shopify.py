"""Shopify pages through the Admin REST API.

Env:
  SHOPIFY_STORE         the store's admin domain, for example your-store.myshopify.com
  SHOPIFY_ADMIN_TOKEN   an Admin API access token from a custom app with read_content and
                        write_content only

Options in seo-engine.toml ([shopify]):
  api_version   Admin API version, default "2026-07"

Reads: GET pages.json?handle=<slug> and GET pages/<id>/metafields.json.
Writes: POST pages.json (created hidden, then shown), PUT pages/<id>.json and PUT
metafields/<id>.json for the page's SEO title and description (global.title_tag and
global.description_tag). `_write` refuses any other write before it is sent, and the connector
never deletes anything. Rollback hides a page it created, or puts back the title, body,
visibility and SEO fields it overwrote.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ..http import Http, Transport
from ..page import Page, render_body
from . import Published

SEO_KEYS = ("title_tag", "description_tag")


def _metafield(key: str, value: str) -> dict[str, str]:
    return {"namespace": "global", "key": key, "value": value, "type": "single_line_text_field"}


class ShopifyPublisher:
    target = "shopify"

    def __init__(self, store: str | None = None, token: str | None = None, api_version: str = "2026-07",
                 transport: Transport | None = None):
        store = store or os.environ.get("SHOPIFY_STORE", "")
        token = token or os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
        if not store or not token:
            raise RuntimeError("set SHOPIFY_STORE and SHOPIFY_ADMIN_TOKEN")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", store):
            raise RuntimeError("SHOPIFY_STORE must be the store's your-store.myshopify.com domain")
        if not re.fullmatch(r"\d{4}-\d{2}", api_version):
            raise RuntimeError("shopify.api_version looks like 2026-07")
        self.base = "https://%s/admin/api/%s" % (store, api_version)
        self.http = Http({"X-Shopify-Access-Token": token}, transport)

    def files(self, page: Page) -> dict[str, str]:
        """The page source for the review pull request: the exact body plus its SEO fields."""
        meta = {"handle": page.slug, "title": page.question, "seo_title": page.question,
                "seo_description": page.description, "canonical": page.canonical,
                "recording": page.recording_id, "transcript_sha256": page.transcript_sha256}
        return {"pages/%s.html" % page.slug: render_body(page),
                "pages/%s.json" % page.slug: json.dumps(meta, indent=2) + "\n"}

    def find(self, handle: str) -> dict[str, Any] | None:
        data = self.http.get(self.base + "/pages.json",
                             {"handle": handle, "fields": "id,handle,title,body_html,published_at"}) or {}
        pages = [p for p in data.get("pages", []) if p.get("handle") == handle]
        if len(pages) > 1:
            raise RuntimeError("more than one Shopify page has the handle %s" % handle)
        return pages[0] if pages else None

    def seo_fields(self, page_id: int) -> dict[str, dict[str, Any]]:
        data = self.http.get("%s/pages/%d/metafields.json" % (self.base, page_id), {"namespace": "global"}) or {}
        return {m["key"]: m for m in data.get("metafields", [])
                if m.get("namespace") == "global" and m.get("key") in SEO_KEYS}

    def publish(self, page: Page) -> Published:
        body = render_body(page)
        seo = {"title_tag": page.question, "description_tag": page.description}
        existing = self.find(page.slug)
        if existing is None:
            created = self._write("POST", self.base + "/pages.json", {"page": {
                "title": page.question, "handle": page.slug, "body_html": body, "published": False,
                "metafields": [_metafield(k, v) for k, v in seo.items()]}})["page"]
            pid = int(created["id"])
            self._write("PUT", "%s/pages/%d.json" % (self.base, pid), {"page": {"id": pid, "published": True}})
            return Published("created", "", {"page_id": pid, "created": True})
        pid = int(existing["id"])
        fields = self.seo_fields(pid)
        current = {k: (fields.get(k) or {}).get("value") for k in SEO_KEYS}
        if (existing.get("title") == page.question and existing.get("body_html") == body and current == seo
                and existing.get("published_at")):
            return Published("unchanged", "")
        undo = {"page_id": pid, "created": False, "title": existing.get("title", ""),
                "body_html": existing.get("body_html", ""), "published": bool(existing.get("published_at")),
                "seo": {k: [m["id"], m.get("value", "")] for k, m in fields.items()}}
        update: dict[str, Any] = {"id": pid, "title": page.question, "body_html": body, "published": True}
        missing = [_metafield(k, seo[k]) for k in SEO_KEYS if k not in fields]
        if missing:
            update["metafields"] = missing
        self._write("PUT", "%s/pages/%d.json" % (self.base, pid), {"page": update})
        for k, m in fields.items():
            if m.get("value") != seo[k]:
                self._set_metafield(int(m["id"]), seo[k])
        return Published("updated", "", undo)

    def rollback(self, published: Published) -> str:
        u = published.undo
        if not u:
            return "nothing to undo"
        pid = int(u["page_id"])
        if u["created"]:
            self._write("PUT", "%s/pages/%d.json" % (self.base, pid), {"page": {"id": pid, "published": False}})
            return "hid the new page"
        self._write("PUT", "%s/pages/%d.json" % (self.base, pid), {"page": {
            "id": pid, "title": u["title"], "body_html": u["body_html"], "published": u["published"]}})
        for mid, value in u["seo"].values():
            self._set_metafield(int(mid), value)
        return "restored the previous title, body and SEO fields"

    def _set_metafield(self, mid: int, value: str) -> None:
        self._write("PUT", "%s/metafields/%d.json" % (self.base, mid),
                    {"metafield": {"id": mid, "value": value, "type": "single_line_text_field"}})

    def _write(self, method: str, url: str, body: dict[str, Any]) -> Any:
        """The only write path: create a page, update a page, or update one metafield's value."""
        base = re.escape(self.base)
        allowed = (
            (method == "POST" and url == self.base + "/pages.json" and set(body) == {"page"}),
            (method == "PUT" and re.fullmatch(base + r"/pages/\d+\.json", url) is not None and set(body) == {"page"}),
            (method == "PUT" and re.fullmatch(base + r"/metafields/\d+\.json", url) is not None
             and set(body) == {"metafield"} and set(body["metafield"]) <= {"id", "value", "type"}),
        )
        if not any(allowed):
            raise PermissionError("refused write: %s %s" % (method, url.split("?")[0]))
        return self.http.request(method, url, json_body=body)
