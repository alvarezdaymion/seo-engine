"""Check a published page the way a search engine sees it.

One fetch, then: HTTP 200 with no redirect, indexable (no noindex in the X-Robots-Tag header or a
robots meta tag), the title contains the question, exactly one canonical link and it is the
expected URL, the meta description matches, the JSON-LD parses and has an Article by the speaker
and a VideoObject for the video, the video embed is present, the speaker's name is on the page,
and the article text still hashes to its data-seo-engine-source marker, which proves the live
body is the approved build.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .http import USER_AGENT
from .page import Page, article_digest, article_text, render_article, video_src
from .text import clean, sha256


@dataclass
class Response:
    status: int
    url: str
    headers: dict[str, str]
    body: str


Fetch = Callable[[str], Response]


def http_fetch(url: str, timeout: float = 20.0) -> Response:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4 * 1024 * 1024).decode("utf-8", "replace")
            return Response(resp.status, resp.geturl(), {k.lower(): v for k, v in resp.headers.items()}, body)
    except urllib.error.HTTPError as e:
        return Response(e.code, url, {k.lower(): v for k, v in (e.headers or {}).items()}, "")


def file_fetch(path: str) -> Response:
    p = Path(path)
    if p.is_dir():
        p = p / "index.html"
    if not p.is_file():
        return Response(404, path, {}, "")
    return Response(200, path, {}, p.read_text())


def fetch_any(target: str) -> Response:
    return http_fetch(target) if target.startswith(("http://", "https://")) else file_fetch(target)


@dataclass
class Expected:
    url: str
    canonical: str
    title: str
    description: str
    speaker: str
    video_src: str
    source_sha256: str


@dataclass
class CheckResult:
    ok: bool
    url: str
    passed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def expected_for(page: Page, fetch_url: str) -> Expected:
    marker, _ = article_digest(render_article(page))
    return Expected(fetch_url, page.canonical, page.question, page.description, page.speaker_name,
                    video_src(page.video_url)[1], marker or "")


class _Head(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title, self.in_title, self.in_ld = "", False, False
        self.canonicals: list[str] = []
        self.descriptions: list[str] = []
        self.robots: list[str] = []
        self.ld: list[str] = []
        self.embeds: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "title":
            self.in_title = True
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonicals.append(a.get("href", "").strip())
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name == "description":
                self.descriptions.append(a.get("content", ""))
            elif name in ("robots", "googlebot"):
                self.robots.append(a.get("content", "").lower())
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self.in_ld = True
            self.ld.append("")
        elif tag in ("iframe", "video", "source") and a.get("src"):
            self.embeds.append(a["src"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag == "script":
            self.in_ld = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        elif self.in_ld:
            self.ld[-1] += data


def _schema_nodes(blocks: list[str]) -> list[dict[str, Any]] | None:
    nodes: list[dict[str, Any]] = []
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            return None
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict):
                nodes.extend(n for n in item.get("@graph", [item]) if isinstance(n, dict))
    return nodes


def check(expected: Expected, fetch: Fetch) -> CheckResult:
    res = CheckResult(False, expected.url)

    def gate(name: str, ok: bool, problem: str) -> None:
        (res.passed if ok else res.problems).append(name if ok else problem)

    try:
        r = fetch(expected.url)
    except Exception as e:
        res.problems.append("fetch failed: %s" % str(e)[:200])
        return res
    if r.status != 200:
        res.problems.append("HTTP %d" % r.status)
        return res
    gate("200", True, "")
    gate("no redirect", r.url.rstrip("/") == expected.url.rstrip("/"), "redirected to %s" % r.url)
    p = _Head()
    p.feed(r.body)
    noindex = "noindex" in r.headers.get("x-robots-tag", "").lower() or any("noindex" in v for v in p.robots)
    gate("indexable", not noindex, "noindex is set")
    gate("title", clean(expected.title) in clean(p.title), "title is %r" % clean(p.title)[:80])
    gate("canonical", p.canonicals == [expected.canonical],
         "canonical is %s" % (", ".join(p.canonicals) or "missing"))
    gate("description", [clean(d) for d in p.descriptions] == [clean(expected.description)],
         "meta description is missing or different")
    nodes = _schema_nodes(p.ld)
    article = next((n for n in nodes or [] if n.get("@type") == "Article"), None)
    video = next((n for n in nodes or [] if n.get("@type") == "VideoObject"), None)
    author = (article or {}).get("author") or {}
    gate("schema", bool(article) and bool(video) and author.get("name") == expected.speaker
         and expected.video_src in (video.get("embedUrl"), video.get("contentUrl")),
         "JSON-LD is invalid" if nodes is None else "JSON-LD lacks an Article by the speaker and the VideoObject")
    gate("video embed", expected.video_src in p.embeds, "video embed is missing")
    marker, text = article_text(r.body)
    gate("speaker", marker is not None and expected.speaker in text, "speaker name is missing")
    gate("source hash", marker == expected.source_sha256 and sha256(text) == expected.source_sha256,
         "article text does not match the approved build" if marker else "source marker is missing")
    res.ok = not res.problems
    return res


def check_until(expected: Expected, fetch: Fetch, attempts: int = 3, wait: float = 10.0,
                sleep: Callable[[float], None] = time.sleep) -> CheckResult:
    """Retry while a CDN or deploy catches up. Returns the last result."""
    result = check(expected, fetch)
    for _ in range(max(0, attempts - 1)):
        if result.ok:
            break
        sleep(wait)
        result = check(expected, fetch)
    return result
