import pytest
from helpers import RULES, SITE, write_answer

from seo_engine.claims import Rule, check, fix, load_rules
from seo_engine.draft import template_draft
from seo_engine.page import build_page, kept_transcript

PACK = load_rules(RULES)
DISCLOSURE = PACK.disclosures[0][1]
STAT = "A faucet that drips once a second wastes about 5 percent of the water a home uses."


def page_for(answer, sentences=None, disclosures=(DISCLOSURE,)):
    kept, _ = kept_transcript(answer)
    flat = [s for para in kept for s in para]
    return build_page(answer, answer_sentences=sentences or template_draft(flat, 2).sentences, slug="faucet",
                      canonical=SITE + "/answers/faucet/",
                      site_name="Test Co", published_on="2026-03-12", disclosures=list(disclosures))


def kinds(findings):
    return sorted((f.kind, f.rule) for f in findings)


def test_a_statistic_without_a_cited_source_is_a_finding(tmp_path):
    findings = check(page_for(write_answer(tmp_path)), PACK)
    assert kinds(findings) == [("needs_source", "percentage")]
    assert findings[0].where == "transcript" and findings[0].text == STAT


def test_the_fix_cites_a_source_approved_with_the_recording(tmp_path):
    answer = write_answer(tmp_path, sources=[{"supports": "5 percent", "title": "Water use (example)",
                                              "url": "https://www.example.org/water-use"}])
    page = page_for(answer)
    actions = fix(page, check(page, PACK), answer, PACK)
    assert check(page, PACK) == []
    assert page.sources == [{"url": "https://www.example.org/water-use", "title": "Water use (example)"}]
    cited = [s for s in page.all_sentences() if s.text == STAT]
    assert cited and all(s.sources == [1] for s in cited)
    assert actions[0].startswith("cited approved source [1]")


def test_without_an_approved_source_the_sentence_is_removed_not_reworded(tmp_path):
    answer = write_answer(tmp_path)
    fixable = "Most faucets can be fixed in under an hour with a replacement cartridge."
    page = page_for(answer, sentences=[STAT, fixable])
    fix(page, check(page, PACK), answer, PACK)
    assert check(page, PACK) == []
    assert all(s.text != STAT for s in page.all_sentences())
    assert page.removed == [STAT] and "5 percent" not in page.description


def test_a_banned_phrase_removes_its_sentence_everywhere(tmp_path):
    transcript = ("Most faucets can be fixed in under an hour with a replacement cartridge. "
                  "Our repairs are guaranteed for life. Call us if the drip comes back.")
    answer = write_answer(tmp_path, transcript=transcript)
    page = page_for(answer, sentences=["Our repairs are guaranteed for life.", "Call us if the drip comes back."])
    findings = check(page, PACK)
    assert {(f.rule, f.where) for f in findings} == {("guarantee", "description"), ("guarantee", "answer")}
    actions = fix(page, findings, answer, PACK)
    assert check(page, PACK) == [] and "guaranteed" not in page.description
    assert all("guaranteed" not in s.text for s in page.all_sentences())
    assert any("never reworded" in a for a in actions) and "rebuilt the meta description" in actions[-1]


def test_a_missing_disclosure_is_added(tmp_path):
    answer = write_answer(tmp_path, transcript="Most faucets can be fixed in under an hour with a new cartridge.")
    page = page_for(answer, disclosures=())
    findings = check(page, PACK)
    assert kinds(findings) == [("missing_disclosure", "general-information")]
    fix(page, findings, answer, PACK)
    assert page.disclosures == [DISCLOSURE] and check(page, PACK) == []


def test_a_banned_phrase_in_the_question_cannot_be_fixed(tmp_path):
    answer = write_answer(tmp_path, question="Who is the cheapest plumber in town?",
                          transcript="We give a written quote before any work starts, every time.")
    page = page_for(answer)
    fix(page, check(page, PACK), answer, PACK)
    left = check(page, PACK)
    assert kinds(left) == [("banned", "superlative")] and left[0].where == "title"


def test_removing_every_answer_sentence_leaves_a_structural_finding(tmp_path):
    answer = write_answer(tmp_path, transcript="Our work is guaranteed. Our work is the best in town.")
    page = page_for(answer, sentences=["Our work is guaranteed.", "Our work is the best in town."])
    fix(page, check(page, PACK), answer, PACK)
    assert ("structure", "answer") in kinds(check(page, PACK))


@pytest.mark.parametrize("text,hit", [
    ("We are the #1 choice.", "#1"),
    ("Item #10 on the list.", None),
    ("Results are GUARANTEED.", "GUARANTEED"),
    ("We guaranteeing nothing here.", None),
])
def test_phrases_match_whole_words_only(text, hit):
    rule = Rule("r", "banned", "", ["#1", "guaranteed"])
    assert rule.hit(text) == hit


def test_a_rule_needs_phrases_or_a_pattern(tmp_path):
    bad = tmp_path / "rules.toml"
    bad.write_text('[[banned]]\nid = "empty"\n')
    with pytest.raises(ValueError):
        load_rules(bad)
