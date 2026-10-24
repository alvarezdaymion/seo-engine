"""Static files: write each page as HTML or Markdown into a site folder.

Options in seo-engine.toml ([static]):
  dir      the site folder, default "site"
  format   "html" (a complete page) or "markdown" (front matter plus the HTML body)
  file     where a page goes inside the folder, default "answers/{slug}/index.html"

The folder can be served as it is, synced to a host, or be a git repo your host deploys from
(set [publish] target = "git" and turn on [review]). Rollback restores the previous file, or
removes a file this run created.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..page import Page, owned_by, render_html, render_markdown
from . import Published


class StaticPublisher:
    target = "static"

    def __init__(self, root: Path, file_pattern: str = "answers/{slug}/index.html", fmt: str = "html"):
        self.root = Path(root).resolve()
        self.file_pattern = file_pattern
        self.fmt = fmt

    def files(self, page: Page) -> dict[str, str]:
        content = render_markdown(page) if self.fmt == "markdown" else render_html(page)
        return {self.file_pattern.format(slug=page.slug): content}

    def _dest(self, rel: str) -> Path:
        dest = (self.root / rel).resolve()
        if not dest.is_relative_to(self.root):
            raise PermissionError("refused to write outside the site folder: %s" % rel)
        return dest

    def publish(self, page: Page) -> Published:
        rel, content = next(iter(self.files(page).items()))
        dest = self._dest(rel)
        previous = dest.read_text() if dest.exists() else None
        if previous is not None and not owned_by(previous, page.recording_id):
            raise PermissionError("refused to replace a file without verified engine ownership: %s" % rel)
        if previous == content:
            return Published("unchanged", str(dest))
        if getattr(self, "before_write", None):
            self.before_write()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(content)
        os.replace(tmp, dest)
        return Published("created" if previous is None else "updated", str(dest),
                         {"path": str(dest), "previous": previous})

    def rollback(self, published: Published) -> str:
        if not published.undo:
            return "nothing to undo"
        path = Path(published.undo["path"])
        if published.undo["previous"] is None:
            path.unlink(missing_ok=True)
            return "removed %s" % path.name
        path.write_text(published.undo["previous"])
        return "restored the previous %s" % path.name


class GitDeployPublisher(StaticPublisher):
    """The site is the reviewed repo: merging the pull request is the publish step."""

    target = "git"

    def publish(self, page: Page) -> Published:
        return Published("deployed by merge", "")

    def rollback(self, published: Published) -> str:
        return "merged work cannot be rolled back automatically; revert the pull request"
