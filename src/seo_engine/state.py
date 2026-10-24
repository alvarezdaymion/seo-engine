"""Engine state on disk.

  state/pages.json          one record per page the engine built: status, URL, recording, hashes,
                            pull request and last live check
  state/pages/<slug>.json   the published build, so a repair republishes exactly what was approved
  state/pages/<slug>.pending.json  a build waiting for review, merge or publish
  state/drafts/<slug>.html  the latest build as HTML, for reading in dry-run mode
  state/ledger.jsonl        one line per action: built, held, opened, merged, published, rolled back
  state/report.json         the last week a report was written for
  state/last-run.json       the summary of the latest run
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from .page import Page, render_html


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class State:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.records: dict[str, dict[str, Any]] = read_json(self.root / "pages.json") or {}

    def save(self) -> None:
        write_json(self.root / "pages.json", self.records)

    def record(self, slug: str) -> dict[str, Any]:
        return self.records.setdefault(slug, {"slug": slug})

    def _page_path(self, slug: str, pending: bool) -> Path:
        return self.root / "pages" / ("%s%s.json" % (slug, ".pending" if pending else ""))

    def page(self, slug: str, pending: bool = False) -> Page | None:
        """The published build, or with pending=True the build waiting for review or publish."""
        data = read_json(self._page_path(slug, pending))
        return Page.from_json(data) if data else None

    def store_page(self, page: Page, pending: bool = False) -> None:
        write_json(self._page_path(page.slug, pending), page.to_json())
        if not pending:
            self._page_path(page.slug, True).unlink(missing_ok=True)

    def store_draft(self, page: Page) -> Path:
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
        except OSError:
            return []
        return [json.loads(x) for x in lines if x.strip()]

    def new_pages_on(self, day: dt.date) -> int:
        return sum(1 for e in self.events()
                   if e.get("event") == "published" and e.get("new") and e.get("day") == day.isoformat())

    def published_urls(self) -> set[str]:
        return {r["url"] for r in self.records.values() if r.get("live") and r.get("url")}
