"""Engine state on disk.

  state/pages.json          one record per page the engine built: status, URL, recording, hashes,
                            pull request and last live check
  state/pages/<slug>.json   the published build, so a repair republishes exactly what was approved
  state/pages/<slug>.pending.json  a build waiting for review, merge or publish
  state/drafts/<slug>.html  the latest build as HTML, for reading in dry-run mode
  state/ledger.jsonl        one line per action: built, held, opened, merged, published, rolled back
  state/report.json         the last week a report was written for
  state/last-run.json       the summary of the latest run
  state/*-writes.json, report-deliveries.json, model-drafts.json  durable operation journals
  state/*.lock              exclusive run and journal locks; interrupted locks require recovery
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .opportunities import norm_url
from .page import Page, owned_by, render_html
from .text import is_slug


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    try:
        data = json.loads(path.read_text())
        if data is None:
            raise ValueError("null state")
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise RuntimeError("state file could not be read: %s; restore it before continuing" % path.name) from None


class RunLock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            message = "another operation is running or was interrupted; reconcile %s" % self.path.name
            raise RuntimeError(message) from None
        os.close(fd)
        return self

    def __exit__(self, *args: Any) -> None:
        self.path.unlink()


class WriteJournal:
    """Record intent before sending; an interrupted write can only be reconciled by reads."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.records = read_json(path) if path and path.exists() else {}
        if not isinstance(self.records, dict):
            raise RuntimeError("write journal is unreadable; reconcile it before continuing")

    def save(self) -> None:
        if self.path:
            write_json(self.path, self.records)

    def perform(self, key: str, call: Callable[[], Any], reconcile: Callable[[], Any] | None = None) -> Any:
        if self.path:
            with RunLock(self.path.with_suffix(".lock")):
                self.records = read_json(self.path) if self.path.exists() else {}
                if not isinstance(self.records, dict):
                    raise RuntimeError("write journal is unreadable; reconcile it before continuing")
                return self._perform(key, call, reconcile)
        return self._perform(key, call, reconcile)

    def _perform(self, key: str, call: Callable[[], Any], reconcile: Callable[[], Any] | None = None) -> Any:
        if key in self.records:
            record = self.records[key]
            if not isinstance(record, dict) or record.get("status") not in ("done", "pending"):
                raise RuntimeError("write journal entry is malformed; reconcile it before continuing")
            if record["status"] == "done":
                return record["result"]
            result = reconcile() if reconcile else None
            if result is None:
                raise RuntimeError("external write outcome is uncertain; reconcile it before continuing")
        else:
            self.records[key] = {"status": "pending"}
            self.save()
            result = call()
        self.records[key] = {"status": "done", "result": result}
        self.save()
        return result


class State:
    def __init__(self, root: Path):
        self.root = Path(root)
        data = read_json(self.root / "pages.json")
        if data is not None and (not isinstance(data, dict) or any(not isinstance(r, dict) for r in data.values())):
            raise RuntimeError("page state is malformed; restore pages.json before continuing")
        self.records: dict[str, dict[str, Any]] = data if data is not None else {}

    def save(self) -> None:
        saved = read_json(self.root / "pages.json")
        saved = {} if saved is None else saved
        if not isinstance(saved, dict) or any(not isinstance(r, dict) for r in saved.values()):
            raise RuntimeError("page ownership state is malformed; restore it before continuing")
        for slug, previous in saved.items():
            if slug not in self.records or self.records[slug].get("recording_id") != previous.get("recording_id"):
                raise PermissionError("page path ownership cannot be changed or removed")
        for slug, record in self.records.items():
            previous = saved.get(slug)
            if record.get("recording_id") and (previous is None or previous.get("url") != record.get("url")):
                self.require_owner(slug, record["recording_id"], record.get("url") or "")
        write_json(self.root / "pages.json", self.records)

    def record(self, slug: str) -> dict[str, Any]:
        return self.records.setdefault(slug, {"slug": slug})

    def require_owner(self, slug: str, recording_id: str, canonical: str = "") -> None:
        if (not isinstance(slug, str) or not is_slug(slug)
                or not isinstance(recording_id, str) or not recording_id):
            raise PermissionError("page path and recording ownership must be known")

        def verify(key: str, data: Any) -> None:
            data = data if isinstance(data, dict) else {}
            url = data.get("canonical", data.get("url"))
            same_url = canonical and isinstance(url, str) and norm_url(url) == norm_url(canonical)
            if key == slug or data.get("slug") == slug or same_url:
                if data.get("recording_id") != recording_id:
                    raise PermissionError("page path %s belongs to another or unknown recording" % slug)

        try:
            saved = read_json(self.root / "pages.json")
        except RuntimeError:
            raise PermissionError("page ownership state is unreadable; restore it before continuing") from None
        if saved is not None and (not isinstance(saved, dict) or any(not isinstance(r, dict) for r in saved.values())):
            raise PermissionError("page ownership state is malformed; restore it before continuing")
        for records in (saved or {}, self.records):
            for key, data in records.items():
                verify(key, data)
        for path in sorted((self.root / "pages").glob("*.json")):
            key = path.name.removesuffix(".json").removesuffix(".pending")
            try:
                data = read_json(path)
            except RuntimeError:
                data = None
            verify(key, data)
        draft = self.root / "drafts" / (slug + ".html")
        try:
            document = draft.read_text()
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            raise PermissionError("saved draft ownership is unreadable") from None
        if not owned_by(document, recording_id):
            raise PermissionError("saved draft belongs to another or unknown recording")

    def _page_path(self, slug: str, pending: bool) -> Path:
        return self.root / "pages" / ("%s%s.json" % (slug, ".pending" if pending else ""))

    def page(self, slug: str, pending: bool = False) -> Page | None:
        """The published build, or with pending=True the build waiting for review or publish."""
        data = read_json(self._page_path(slug, pending))
        if data is None:
            return None
        try:
            if not isinstance(data, dict):
                raise ValueError("expected a page object")
            return Page.from_json(data)
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("saved build is malformed; restore it before continuing") from None

    def store_page(self, page: Page, pending: bool = False) -> None:
        self.require_owner(page.slug, page.recording_id, page.canonical)
        write_json(self._page_path(page.slug, pending), page.to_json())
        if not pending:
            self._page_path(page.slug, True).unlink(missing_ok=True)

    def store_draft(self, page: Page) -> Path:
        self.require_owner(page.slug, page.recording_id, page.canonical)
        path = self.root / "drafts" / ("%s.html" % page.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_html(page))
        return path

    def log(self, event: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = {"at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), **event}
        with (self.root / "ledger.jsonl").open("a") as f:
            f.write(json.dumps(line, default=str) + "\n")

    def events(self) -> list[dict[str, Any]]:
        try:
            lines = (self.root / "ledger.jsonl").read_text().splitlines()
        except FileNotFoundError:
            return []
        except OSError:
            raise RuntimeError("ledger.jsonl could not be read; restore it before continuing") from None
        try:
            events = [json.loads(x) for x in lines if x.strip()]
            if any(not isinstance(e, dict) for e in events):
                raise ValueError("expected event objects")
            return events
        except ValueError:
            raise RuntimeError("ledger.jsonl is malformed; restore it before continuing") from None

    def new_pages_on(self, day: dt.date) -> int:
        return sum(1 for e in self.events()
                   if e.get("event") == "published" and e.get("new") and e.get("day") == day.isoformat())

    def published_urls(self) -> set[str]:
        return {r["url"] for r in self.records.values() if r.get("live") and r.get("url")}
