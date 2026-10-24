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
visibility and SEO fields it overwrote. Updates stay hidden until every field succeeds, and
the engine persists the undo snapshot before mutations.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

from ..http import Http, Transport, credential
from ..page import Page, owned_by, render_body
from . import Published

SEO_KEYS = ("title_tag", "description_tag")


def _metafield(key: str, value: str) -> dict[str, str]:
    return {"namespace": "global", "key": key, "value": value, "type": "single_line_text_field"}


class ShopifyPreview:
    target = "shopify"

    def files(self, page: Page) -> dict[str, str]:
        """The exact body and SEO fields for preview and review, without credentials."""
        meta = {"handle": page.slug, "title": page.question, "seo_title": page.question,
                "seo_description": page.description, "canonical": page.canonical,
                "recording": page.recording_id, "transcript_sha256": page.transcript_sha256,
                "revision_sha256": page.revision_sha256}
        return {"pages/%s.html" % page.slug: render_body(page),
                "pages/%s.json" % page.slug: json.dumps(meta, indent=2) + "\n"}


class PublishFailed(RuntimeError):
    def __init__(self, recovery: str):
        super().__init__("Shopify publication did not complete; " + recovery)
        self.recovery = recovery


class ShopifyPublisher(ShopifyPreview):
    def __init__(self, store: str | None = None, token: str | None = None, api_version: str = "2026-07",
                 transport: Transport | None = None):
        store = store or os.environ.get("SHOPIFY_STORE", "")
        token = token or os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
        if not store or not token:
            raise RuntimeError("set SHOPIFY_STORE and SHOPIFY_ADMIN_TOKEN")
        token = credential(token, "SHOPIFY_ADMIN_TOKEN")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", store):
            raise RuntimeError("SHOPIFY_STORE must be the store's your-store.myshopify.com domain")
        if not re.fullmatch(r"\d{4}-\d{2}", api_version):
            raise RuntimeError("shopify.api_version looks like 2026-07")
        self.base = "https://%s/admin/api/%s" % (store, api_version)
        self.http = Http({"X-Shopify-Access-Token": token}, transport)
        self.save_recovery: Callable[[dict[str, Any]], None] = lambda undo: None
        self.before_write: Callable[[], None] = lambda: None
        self.before_restore: Callable[[dict[str, Any]], None] | None = None
        self.recovering = False
        self.recovery_undo: dict[str, Any] = {}

    def find(self, handle: str) -> dict[str, Any] | None:
        data = self.http.get(self.base + "/pages.json",
                             {"handle": handle, "fields": "id,handle,title,body_html,published_at"})
        if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
            raise RuntimeError("Shopify page lookup is unavailable")
        pages = data["pages"]
        if any(not isinstance(p, dict) or p.get("handle") != handle
               or not isinstance(p.get("body_html"), str) or not p.get("id") for p in pages):
            raise RuntimeError("Shopify page lookup is malformed")
        if len(pages) > 1:
            raise RuntimeError("more than one Shopify page has the handle %s" % handle)
        return pages[0] if pages else None

    def seo_fields(self, page_id: int) -> dict[str, dict[str, Any]]:
        data = self.http.get("%s/pages/%d/metafields.json" % (self.base, page_id), {"namespace": "global"})
        if not isinstance(data, dict) or not isinstance(data.get("metafields"), list):
            raise RuntimeError("Shopify SEO fields are unavailable")
        return {m["key"]: m for m in data.get("metafields", [])
                if m.get("namespace") == "global" and m.get("key") in SEO_KEYS}

    def publish(self, page: Page) -> Published:
        body = render_body(page)
        seo = {"title_tag": page.question, "description_tag": page.description}
        existing = self.find(page.slug)
        if existing is None:
            undo = {"created": True, "handle": page.slug}
            self.save_recovery(undo)
            try:
                created = self._write("POST", self.base + "/pages.json", {"page": {
                    "title": page.question, "handle": page.slug, "body_html": body, "published": False,
                    "metafields": [_metafield(k, v) for k, v in seo.items()]}})["page"]
                if (not isinstance(created, dict) or type(created.get("id")) is not int
                        or created.get("handle") != page.slug or created.get("body_html") != body):
                    raise RuntimeError("Shopify creation response is unverified")
                pid = int(created["id"])
                self._verify_hidden(pid, page.slug, page.question, body, seo)
                undo["page_id"] = pid
                self.save_recovery(undo)
                self._write("PUT", "%s/pages/%d.json" % (self.base, pid), {"page": {"id": pid, "published": True}})
                return Published("created", "", undo)
            except Exception:
                try:
                    found = self.find(page.slug)
                    if found and found["body_html"] == body and owned_by(found["body_html"], page.recording_id):
                        undo["page_id"] = int(found["id"])
                        self.save_recovery(undo)
                except Exception:
                    pass
                raise PublishFailed(self._recover(undo)) from None
        if not owned_by(existing["body_html"], page.recording_id):
            raise PermissionError("refused to replace a Shopify page without verified engine ownership")
        pid = int(existing["id"])
        fields = self.seo_fields(pid)
        current = {k: (fields.get(k) or {}).get("value") for k in SEO_KEYS}
        if (existing.get("title") == page.question and existing.get("body_html") == body and current == seo
                and existing.get("published_at")):
            return Published("unchanged", "")
        undo = {"page_id": pid, "handle": page.slug, "created": False, "title": existing.get("title", ""),
                "body_html": existing.get("body_html", ""), "published": bool(existing.get("published_at")),
                "seo": {k: [m["id"], m.get("value", "")] for k, m in fields.items()}}
        self.save_recovery(undo)
        update: dict[str, Any] = {"id": pid, "title": page.question, "body_html": body, "published": False}
        missing = [_metafield(k, seo[k]) for k in SEO_KEYS if k not in fields]
        if missing:
            update["metafields"] = missing
        try:
            self._write("PUT", "%s/pages/%d.json" % (self.base, pid), {"page": update})
            for k, m in fields.items():
                if m.get("value") != seo[k]:
                    self._set_metafield(int(m["id"]), seo[k])
            self._verify_hidden(pid, page.slug, page.question, body, seo)
            self._write("PUT", "%s/pages/%d.json" % (self.base, pid), {"page": {"id": pid, "published": True}})
        except Exception:
            raise PublishFailed(self._recover(undo)) from None
        return Published("updated", "", undo)

    def _verify_hidden(self, pid: int, handle: str, title: str, body: str, seo: dict[str, str]) -> None:
        current = self.find(handle)
        fields = self.seo_fields(pid)
        if (not current or current.get("id") != pid or current.get("title") != title
                or current.get("body_html") != body or "published_at" not in current or current["published_at"]
                or {k: fields.get(k, {}).get("value") for k in SEO_KEYS} != seo):
            raise RuntimeError("Shopify has not confirmed the hidden page and SEO fields")

    def _recover(self, undo: dict[str, Any]) -> str:
        try:
            return self.rollback(Published("partial", undo=undo))
        except Exception:
            return "recovery is incomplete; reconcile the saved publication before continuing"

    def rollback(self, published: Published) -> str:
        self.recovering = True
        self.recovery_undo = published.undo
        try:
            return self._rollback(published)
        finally:
            self.recovering = False
            self.recovery_undo = {}

    def _rollback(self, published: Published) -> str:
        u = published.undo
        if not u:
            return "nothing to undo"
        if not u.get("page_id"):
            raise RuntimeError("creation outcome is uncertain; reconcile the saved handle")
        pid = int(u["page_id"])
        if u["created"]:
            self._write("PUT", "%s/pages/%d.json" % (self.base, pid), {"page": {"id": pid, "published": False}})
            hidden = self.find(u["handle"])
            if not hidden or hidden.get("id") != pid or "published_at" not in hidden or hidden["published_at"]:
                raise RuntimeError("Shopify has not confirmed that the new page is hidden")
            return "hid the new page"
        self._write("PUT", "%s/pages/%d.json" % (self.base, pid), {"page": {
            "id": pid, "title": u["title"], "body_html": u["body_html"], "published": False}})
        for mid, value in u["seo"].values():
            self._set_metafield(int(mid), value)
        for key, meta in self.seo_fields(pid).items():
            if key not in u["seo"]:
                self._set_metafield(int(meta["id"]), "")
        seo = {k: u["seo"][k][1] if k in u["seo"] else "" for k in SEO_KEYS}
        self._verify_hidden(pid, u["handle"], u["title"], u["body_html"], seo)
        if u["published"]:
            if self.before_restore is None:
                return "restored the previous title, body and SEO fields; kept hidden without a previous build gate"
            self._write("PUT", "%s/pages/%d.json" % (self.base, pid), {"page": {"id": pid, "published": True}})
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
        if self.recovering and (body.get("page") or {}).get("published") is True:
            if self.before_restore is None:
                raise RuntimeError("previous build release gates are unavailable; kept hidden")
            self.before_restore(self.recovery_undo)
        elif not self.recovering:
            self.before_write()
        return self.http.request(method, url, json_body=body)
