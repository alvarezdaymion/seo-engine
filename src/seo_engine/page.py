"""Build a page from an approved recorded answer, and render it.

A page is the recorded question as the H1, the speaker's name and role, a short direct answer
taken from the transcript, the video, the full transcript minus the lines cut from the approved
edit, cited sources and the required disclosures. `build_page` refuses to run without an
approved recording, and refuses answer sentences that differ from the transcript after tidying.
There is no code path that writes a page from a keyword alone.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any

from .answers import RecordedAnswer, require_approved
from .text import paragraphs, phrase_in, sentences, sha256, tidy

DESCRIPTION_MAX = 160


@dataclass
class Sentence:
    text: str
    sources: list[int] = field(default_factory=list)  # 1-based positions in Page.sources


@dataclass
class Page:
    slug: str
    question: str
    description: str
    canonical: str
    site_name: str
    speaker_name: str
    speaker_role: str
    recorded_on: str
    published_on: str
    video_url: str
    thumbnail_url: str
    answer: list[Sentence]
    transcript: list[list[Sentence]]
    sources: list[dict[str, str]]
    disclosures: list[str]
    recording_id: str
    transcript_sha256: str
    revision_sha256: str = ""
    draft_method: str = "template"
    removed: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Page:
        d = dict(d)
        d["answer"] = [Sentence(**s) for s in d["answer"]]
        d["transcript"] = [[Sentence(**s) for s in para] for para in d["transcript"]]
        return cls(**d)

    def all_sentences(self) -> list[Sentence]:
        return self.answer + [s for para in self.transcript for s in para]


def kept_transcript(answer: RecordedAnswer) -> tuple[list[list[str]], int]:
    """Transcript paragraphs as tidied sentences, without the lines cut from the approved edit."""
    kept, cut = [], 0
    for para in paragraphs(answer.transcript):
        out = []
        for s in sentences(para):
            if any(phrase_in(s, c) or phrase_in(c, s) for c in answer.cut):
                cut += 1
                continue
            out.append(tidy(s))
        if out:
            kept.append(out)
    return kept, cut


def describe(answer: list[Sentence]) -> str:
    """Meta description: whole answer sentences up to 160 characters."""
    out = ""
    for s in answer:
        candidate = (out + " " + s.text).strip()
        if len(candidate) > DESCRIPTION_MAX:
            break
        out = candidate
    if not out and answer:
        out = answer[0].text[:DESCRIPTION_MAX - 3].rsplit(" ", 1)[0] + "..."
    return out


def build_page(answer: RecordedAnswer | None, *, answer_sentences: list[str], slug: str, canonical: str,
               site_name: str, published_on: str, disclosures: list[str], draft_method: str = "template") -> Page:
    answer = require_approved(answer)
    kept, _ = kept_transcript(answer)
    allowed = [s for para in kept for s in para]
    last = -1
    for s in answer_sentences:
        try:
            last = allowed.index(tidy(s), last + 1)
        except ValueError:
            raise ValueError("answer sentence is not in the approved transcript in its original order") from None
    if not answer_sentences:
        raise ValueError("the answer section is empty")
    direct = [Sentence(tidy(s)) for s in answer_sentences]
    return Page(
        slug=slug, question=answer.question, description=describe(direct), canonical=canonical,
        site_name=site_name, speaker_name=answer.speaker_name, speaker_role=answer.speaker_role,
        recorded_on=answer.recorded_on, published_on=published_on, video_url=answer.video_url,
        thumbnail_url=answer.thumbnail_url or youtube_thumbnail(answer.video_url), answer=direct,
        transcript=[[Sentence(s) for s in para] for para in kept], sources=[], disclosures=list(disclosures),
        recording_id=answer.id, transcript_sha256=answer.transcript_sha256,
        revision_sha256=answer.revision_sha256, draft_method=draft_method)


# ---- video -------------------------------------------------------------------------------

_YOUTUBE = re.compile(r"^https://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)"
                      r"([A-Za-z0-9_-]{11})")
_VIMEO = re.compile(r"^https://(?:www\.)?vimeo\.com/(\d+)")


def video_src(url: str) -> tuple[str, str]:
    """("iframe", embed URL) for YouTube and Vimeo, ("file", URL) for anything else."""
    m = _YOUTUBE.match(url)
    if m:
        return "iframe", "https://www.youtube-nocookie.com/embed/" + m.group(1)
    m = _VIMEO.match(url)
    if m:
        return "iframe", "https://player.vimeo.com/video/" + m.group(1)
    return "file", url


def youtube_thumbnail(url: str) -> str:
    m = _YOUTUBE.match(url)
    return "https://i.ytimg.com/vi/%s/hqdefault.jpg" % m.group(1) if m else ""


# ---- rendering ---------------------------------------------------------------------------

def _e(value: str) -> str:
    return html.escape(value, quote=True)


def json_ld(page: Page) -> dict[str, Any]:
    kind, src = video_src(page.video_url)
    video: dict[str, Any] = {"@type": "VideoObject", "@id": page.canonical + "#video", "name": page.question,
                             "description": page.description, "uploadDate": page.recorded_on or page.published_on}
    if page.thumbnail_url:
        video["thumbnailUrl"] = page.thumbnail_url
    video["embedUrl" if kind == "iframe" else "contentUrl"] = src
    author: dict[str, Any] = {"@type": "Person", "name": page.speaker_name}
    if page.speaker_role:
        author["jobTitle"] = page.speaker_role
    article = {"@type": "Article", "headline": page.question, "description": page.description,
               "author": author, "datePublished": page.published_on, "mainEntityOfPage": page.canonical,
               "publisher": {"@type": "Organization", "name": page.site_name}, "video": {"@id": video["@id"]}}
    return {"@context": "https://schema.org", "@graph": [article, video]}


def json_ld_script(page: Page) -> str:
    data = json.dumps(json_ld(page), ensure_ascii=False).replace("<", "\\u003c")
    return '<script type="application/ld+json">%s</script>' % data


def _sentence_html(s: Sentence) -> str:
    refs = "".join('<sup><a href="#source-%d">[%d]</a></sup>' % (n, n) for n in s.sources)
    return _e(s.text) + refs


def _video_html(page: Page) -> str:
    kind, src = video_src(page.video_url)
    if kind == "iframe":
        return ('<iframe src="%s" title="%s" loading="lazy" allowfullscreen '
                'allow="encrypted-media; picture-in-picture"></iframe>' % (_e(src), _e(page.question)))
    poster = ' poster="%s"' % _e(page.thumbnail_url) if page.thumbnail_url else ""
    return '<video controls preload="metadata" src="%s"%s></video>' % (_e(src), poster)


def render_article(page: Page) -> str:
    byline = "Answered by <strong>%s</strong>" % _e(page.speaker_name)
    if page.speaker_role:
        byline += ", " + _e(page.speaker_role)
    if page.recorded_on:
        byline += ". Recorded %s." % _e(page.recorded_on)
    parts = [
        "<h1>%s</h1>" % _e(page.question),
        '<p class="byline">%s</p>' % byline,
        '<section class="answer"><p>%s</p></section>' % " ".join(_sentence_html(s) for s in page.answer),
        '<figure class="video">%s</figure>' % _video_html(page),
        '<section class="transcript"><h2>Full answer</h2>%s</section>'
        % "".join("<p>%s</p>" % " ".join(_sentence_html(s) for s in para) for para in page.transcript),
    ]
    if page.sources:
        items = "".join('<li id="source-%d"><a href="%s" rel="nofollow">%s</a></li>' % (i, _e(s["url"]), _e(s["title"]))
                        for i, s in enumerate(page.sources, 1))
        parts.append('<section class="sources"><h2>Sources</h2><ol>%s</ol></section>' % items)
    for d in page.disclosures:
        parts.append('<aside class="disclosure"><p>%s</p></aside>' % _e(d))
    unsigned = '<article data-seo-engine-source="" data-seo-engine-recording="%s">\n%s\n</article>' % (
        _e(page.recording_id), "\n".join(parts))
    _, digest = article_digest(unsigned)
    return unsigned.replace('data-seo-engine-source=""', 'data-seo-engine-source="%s"' % digest, 1)


def render_body(page: Page) -> str:
    """The article and its JSON-LD, for a CMS page body (Shopify) or a Markdown file."""
    return render_article(page) + "\n" + json_ld_script(page) + "\n"


STYLE = ("body{font-family:system-ui,sans-serif;line-height:1.6;margin:0;padding:0 16px;color:#1a1a1a}"
         "main{max-width:720px;margin:2rem auto}.byline{color:#555}"
         "video,iframe{width:100%;aspect-ratio:16/9;border:0}.disclosure{font-size:.9rem;color:#555}")


def render_html(page: Page) -> str:
    og_image = '\n<meta property="og:image" content="%s">' % _e(page.thumbnail_url) if page.thumbnail_url else ""
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s | %s</title>
<meta name="description" content="%s">
<link rel="canonical" href="%s">
<meta property="og:type" content="article">
<meta property="og:title" content="%s">
<meta property="og:description" content="%s">
<meta property="og:url" content="%s">%s
%s
<style>%s</style>
</head>
<body>
<main>
%s
</main>
</body>
</html>
""" % (_e(page.question), _e(page.site_name), _e(page.description), _e(page.canonical), _e(page.question),
       _e(page.description), _e(page.canonical), og_image, json_ld_script(page), STYLE, render_article(page))


def render_markdown(page: Page) -> str:
    """Front matter for Hugo, Jekyll or Eleventy, then the HTML body (the generator must allow raw HTML)."""
    front = {"title": page.question, "description": page.description, "canonical": page.canonical,
             "date": page.published_on, "author": page.speaker_name, "recording": page.recording_id}
    lines = ["---"] + ["%s: %s" % (k, json.dumps(v, ensure_ascii=False)) for k, v in front.items()] + ["---", ""]
    return "\n".join(lines) + render_body(page)


# ---- the source marker -------------------------------------------------------------------

class _ArticleText(HTMLParser):
    """Text inside the first <article> that carries data-seo-engine-source, scripts excluded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.marker: str | None = None
        self.recording: str | None = None
        self.depth = 0
        self.skip = 0
        self.done = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        a = dict(attrs)
        if self.depth == 0 and tag == "article" and "data-seo-engine-source" in a:
            self.marker, self.depth = a["data-seo-engine-source"] or "", 1
            self.recording = a.get("data-seo-engine-recording")
        elif self.depth and tag == "article":
            self.depth += 1
        elif self.depth and tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if not self.depth or self.done:
            return
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag == "article":
            self.depth -= 1
            self.done = self.depth == 0

    def handle_data(self, data: str) -> None:
        if self.depth and not self.skip and not self.done:
            self.parts.append(data)


def article_text(document: str) -> tuple[str | None, str]:
    """(the marker value, the article's visible text with single spaces). Marker is None without an article."""
    p = _ArticleText()
    p.feed(document)
    return p.marker, " ".join(" ".join(p.parts).split())


def article_digest(document: str) -> tuple[str | None, str | None]:
    """(the marker value, SHA-256 of the article's visible text). (None, None) without an article."""
    marker, text = article_text(document)
    return (None, None) if marker is None else (marker, sha256(text))


def owned_by(document: str, recording_id: str) -> bool:
    parser = _ArticleText()
    parser.feed(document)
    marker, digest = article_digest(document)
    return bool(marker and marker == digest and parser.done and parser.recording == recording_id)
