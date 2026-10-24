"""Static files: write each page as HTML or Markdown into a site folder.

Options in seo-engine.toml ([static]):
  dir      the site folder, default "site"
  format   "html" (a complete page) or "markdown" (front matter plus the HTML body)
  file     where a page goes inside the folder, default "answers/{slug}/index.html"

The folder can be served as it is, synced to a host, or be a git repo your host deploys from
(set [publish] target = "git" and turn on [review]). Rollback restores a freshly approved
previous build, or removes the verified file this run wrote. Static paths cannot use symlinks.
"""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from ..page import Page, owned_by, render_html, render_markdown
from . import Published


class StaticPublisher:
    target = "static"

    def __init__(self, root: Path, file_pattern: str = "answers/{slug}/index.html", fmt: str = "html"):
        self.root = Path(root).resolve()
        self.file_pattern = file_pattern
        self.fmt = fmt
        self.owned_files: dict[str, str] = {}

    def files(self, page: Page) -> dict[str, str]:
        content = render_markdown(page) if self.fmt == "markdown" else render_html(page)
        return {self.file_pattern.format(slug=page.slug): content}

    def _dest(self, rel: str) -> Path:
        path = Path(rel)
        if path.is_absolute() or ".." in path.parts or not path.name:
            raise PermissionError("refused to write outside the site folder: %s" % rel)
        return self.root / path

    @staticmethod
    def _identity(info: os.stat_result) -> list[int]:
        return [info.st_dev, info.st_ino]

    @contextmanager
    def _parent(self, rel: str, *, create: bool = False):
        dest = self._dest(rel)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(dest.anchor, flags)
        directories = [self._identity(os.fstat(fd))]
        try:
            for part in dest.parent.parts[1:]:
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, dir_fd=fd)
                    child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
                directories.append(self._identity(os.fstat(fd)))
            yield fd, directories
        except OSError as e:
            raise PermissionError("static destination is unavailable or uses a symlink: %s" % rel) from e
        finally:
            os.close(fd)

    def _read(self, fd: int, name: str) -> tuple[str | None, list[int] | None]:
        try:
            source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None, None
        with os.fdopen(source, encoding="utf-8") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise PermissionError("static destination must be a regular file")
            return f.read(), self._identity(info)

    @contextmanager
    def _temporary(self, fd: int, content: str):
        name = ".seo-engine-%s.tmp" % secrets.token_hex(16)
        target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        identity = self._identity(os.fstat(target))
        try:
            with os.fdopen(target, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            yield name, identity
        finally:
            try:
                if self._identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) == identity:
                    os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass

    def _verify_parent(self, rel: str, directories: list[list[int]]) -> None:
        with self._parent(rel) as (_, current):
            if current != directories:
                raise PermissionError("static destination directory changed; reconcile publication")

    def _verify_file(self, fd: int, binding: dict) -> None:
        content, identity = self._read(fd, Path(binding["relative"]).name)
        if (identity != binding["file"] or content is None
                or sha256(content.encode()).hexdigest() != binding["sha256"]):
            raise PermissionError("static destination changed; reconcile publication")

    def verify(self, published: Published) -> None:
        binding = published.binding
        if (binding.get("root") != str(self.root)
                or binding.get("path") != str(self._dest(binding["relative"]))):
            raise PermissionError("static destination binding is missing or mismatched")
        with self._parent(binding["relative"]) as (fd, directories):
            if directories != binding["directories"]:
                raise PermissionError("static destination directory changed; reconcile publication")
            self._verify_file(fd, binding)

    def publish(self, page: Page) -> Published:
        rel, content = next(iter(self.files(page).items()))
        dest = self._dest(rel)
        if rel in self.owned_files and self.owned_files[rel] != str(dest):
            raise PermissionError("static destination differs from its saved ownership binding")
        with self._parent(rel, create=True) as (fd, directories):
            previous, identity = self._read(fd, dest.name)
            known = self.owned_files.get(rel) == str(dest)
            if previous is not None and not known and not owned_by(previous, page.recording_id):
                raise PermissionError("refused to replace a file without verified engine ownership: %s" % rel)
            binding = {"root": str(self.root), "relative": rel, "path": str(dest),
                       "directories": directories, "file": identity, "sha256": sha256(content.encode()).hexdigest()}
            if previous == content:
                return Published("unchanged", str(dest), binding=binding)
            with self._temporary(fd, content) as (tmp, written):
                binding["file"] = written
                undo = {"path": str(dest), "relative": rel, "previous": previous, "binding": binding}
                if getattr(self, "save_recovery", None):
                    self.save_recovery(undo)
                if getattr(self, "before_write", None):
                    self.before_write()
                self._verify_parent(rel, directories)
                if self._read(fd, dest.name) != (previous, identity):
                    raise PermissionError("static destination changed before publication")
                if self._identity(os.stat(tmp, dir_fd=fd, follow_symlinks=False)) != written:
                    raise PermissionError("static temporary file changed before publication")
                os.replace(tmp, dest.name, src_dir_fd=fd, dst_dir_fd=fd)
            return Published("created" if previous is None else "updated", str(dest), undo, binding)

    def rollback(self, published: Published) -> str:
        if not published.undo:
            return "nothing to undo"
        self.verify(published)
        undo, binding = published.undo, published.binding
        rel = binding["relative"]
        name = Path(rel).name
        with self._parent(rel) as (fd, directories):
            if directories != binding["directories"]:
                raise PermissionError("static destination directory changed; reconcile publication")
            if undo["previous"] is not None:
                with self._temporary(fd, undo["previous"]) as (tmp, identity):
                    try:
                        if not getattr(self, "before_restore", None):
                            raise RuntimeError("previous approved build cannot be verified")
                        self.before_restore(undo)
                    except Exception:
                        pass
                    else:
                        self._verify_parent(rel, directories)
                        self._verify_file(fd, binding)
                        if self._identity(os.stat(tmp, dir_fd=fd, follow_symlinks=False)) != identity:
                            raise PermissionError("static temporary file changed before restoration")
                        os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
                        return "restored the previous %s" % name
            self._verify_parent(rel, directories)
            self._verify_file(fd, binding)
            os.unlink(name, dir_fd=fd)
            undo["unpublished"] = True
            if getattr(self, "save_recovery", None):
                self.save_recovery(undo)
            return "removed %s%s" % (name, "; previous build failed release gates" if undo["previous"] else "")


class GitDeployPublisher(StaticPublisher):
    """The site is the reviewed repo: merging the pull request is the publish step."""

    target = "git"

    def publish(self, page: Page) -> Published:
        return Published("deployed by merge", "")

    def rollback(self, published: Published) -> str:
        return "merged work cannot be rolled back automatically; revert the pull request"
