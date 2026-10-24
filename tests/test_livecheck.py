import json
import re

import pytest
from helpers import SITE, write_answer

from seo_engine.livecheck import Response, check, check_until, expected_for, file_fetch
from seo_engine.page import build_page, json_ld, kept_transcript, render_body, render_html, render_markdown, video_src

URL = SITE + "/answers/faucet/"


def make_page(tmp_path, **meta):
    answer = write_answer(tmp_path, **meta)
    kept, _ = kept_transcript(answer)
    return build_page(answer, answer_sentences=kept[0][1:3], slug="faucet", canonical=URL, site_name="Test Co",
                      published_on="2026-03-12", disclosures=["General information only."])


def serve(body, status=200, headers=None, url=URL):
    return lambda u: Response(status, url, headers or {}, body)


def test_a_rendered_page_passes_every_check(tmp_path):
    page = make_page(tmp_path)
    res = check(expected_for(page, URL), serve(render_html(page)))
    assert res.ok, res.problems
    assert res.passed == ["200", "no redirect", "indexable", "title", "canonical", "description", "schema",
                          "video embed", "speaker", "source hash"]


def test_the_page_carries_the_speaker_video_schema_and_disclosure(tmp_path):
    page = make_page(tmp_path)
    html = render_html(page)
    assert "<h1>Why is my kitchen faucet dripping?</h1>" in html
    assert "Answered by <strong>Sam Placeholder</strong>, Plumber" in html
    assert '<link rel="canonical" href="%s">' % URL in html
    assert '<video controls preload="metadata" src="https://videos.example.com/ra-100.mp4"' in html
    assert "General information only." in html
    graph = json_ld(page)["@graph"]
    assert graph[0]["author"] == {"@type": "Person", "name": "Sam Placeholder", "jobTitle": "Plumber"}
    assert graph[1]["contentUrl"] == "https://videos.example.com/ra-100.mp4"


@pytest.mark.parametrize("edit,problem", [
    (lambda h: h.replace("<title>", '<meta name="robots" content="noindex"><title>'), "noindex is set"),
    (lambda h: h.replace(URL + '">', SITE + '/elsewhere/">', 1), "canonical is"),
    (lambda h: re.sub(r'<meta name="description"[^>]*>', "", h), "meta description"),
    (lambda h: h.replace('{"@context"', '{"@context" broken', 1), "JSON-LD is invalid"),
    (lambda h: re.sub(r"<video[^>]*></video>", "", h), "video embed is missing"),
    (lambda h: h.replace("worn out", "worn out, guaranteed", 1).replace("worn out.", "worn out, guaranteed."),
     "does not match the approved build"),
    (lambda h: h.replace("data-seo-engine-source", "data-other"), "source marker is missing"),
])
def test_each_live_problem_is_caught(tmp_path, edit, problem):
    page = make_page(tmp_path)
    res = check(expected_for(page, URL), serve(edit(render_html(page))))
    assert not res.ok and any(problem in p for p in res.problems), res.problems


def test_noindex_header_status_and_redirects_are_caught(tmp_path):
    page = make_page(tmp_path)
    exp, html = expected_for(page, URL), render_html(page)
    assert "noindex is set" in check(exp, serve(html, headers={"x-robots-tag": "noindex"})).problems
    assert check(exp, serve("", status=404)).problems == ["HTTP 404"]
    assert any("redirected" in p for p in check(exp, serve(html, url=SITE + "/")).problems)


def test_the_live_check_retries_until_the_deploy_lands(tmp_path):
    page = make_page(tmp_path)
    responses = iter([Response(404, URL, {}, ""), Response(200, URL, {}, render_html(page))])
    waits = []
    res = check_until(expected_for(page, URL), lambda u: next(responses), attempts=3, wait=5, sleep=waits.append)
    assert res.ok and waits == [5]


def test_a_cms_body_and_markdown_keep_the_marker_and_schema(tmp_path):
    page = make_page(tmp_path)
    marker = expected_for(page, URL).source_sha256
    for out in (render_body(page), render_markdown(page)):
        assert 'data-seo-engine-source="%s"' % marker in out and "application/ld+json" in out
    front = render_markdown(page).split("---")[1]
    assert 'title: "Why is my kitchen faucet dripping?"' in front and 'canonical: "%s"' % URL in front


def test_youtube_and_vimeo_embeds(tmp_path):
    assert video_src("https://www.youtube.com/watch?v=abcdefghijk") == (
        "iframe", "https://www.youtube-nocookie.com/embed/abcdefghijk")
    assert video_src("https://youtu.be/abcdefghijk")[1].endswith("/embed/abcdefghijk")
    assert video_src("https://vimeo.com/12345") == ("iframe", "https://player.vimeo.com/video/12345")
    page = make_page(tmp_path, video_url="https://www.youtube.com/watch?v=abcdefghijk")
    video = json_ld(page)["@graph"][1]
    assert video["embedUrl"].endswith("/embed/abcdefghijk") and video["thumbnailUrl"].endswith("hqdefault.jpg")
    assert check(expected_for(page, URL), serve(render_html(page))).ok


def test_json_ld_cannot_close_its_script_tag(tmp_path):
    page = make_page(tmp_path, question="Why does </script><b>this</b> matter?")
    html = render_html(page)
    ld = html.split('<script type="application/ld+json">')[1].split("</script>")[0]
    assert json.loads(ld)["@graph"][0]["headline"] == "Why does </script><b>this</b> matter?"
    assert "<h1>Why does &lt;/script&gt;&lt;b&gt;this&lt;/b&gt; matter?</h1>" in html


def test_file_fetch(tmp_path):
    (tmp_path / "index.html").write_text("<p>hi</p>")
    assert file_fetch(str(tmp_path)).status == 200
    assert file_fetch(str(tmp_path / "missing.html")).status == 404
