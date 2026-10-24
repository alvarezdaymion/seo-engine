import json
import os

import pytest

from seo_engine import config
from seo_engine.cli import main


def test_demo_command_prints_the_refusals_the_build_and_the_report(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert 'refused: no recorded answer covers it' in out
    assert 'refused: recording ra-002 is not approved' in out
    assert "BUILD: from recording ra-001 (Alex Example)" in out
    assert "Claim check again: passed" in out and "Merged." in out
    assert "source hash)" in out and "Weekly report: seo-engine-demo/reports/2026-03-02.md" in out
    assert "Result: published" in out


def test_init_check_and_approve(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("GSC_SERVICE_ACCOUNT_FILE", "GSC_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert main(["init"]) == 0
    assert (tmp_path / "seo-engine.toml").exists() and (tmp_path / "rules.toml").exists()
    assert main(["check"]) == 1  # no Search Console credentials yet
    out = capsys.readouterr().out
    assert "Config OK" in out and "Not ready: fix search." in out
    assert 'example-answer   not approved: status is "recorded"' in out

    assert main(["approve", "example-answer"]) == 1  # the example has no speaker or video yet
    meta = json.loads((tmp_path / "answers" / "example-answer.json").read_text())
    meta.update(speaker={"name": "Pat Placeholder", "role": "Owner"}, video_url="https://videos.example.com/a.mp4")
    (tmp_path / "answers" / "example-answer.json").write_text(json.dumps(meta))
    assert main(["approve", "example-answer", "--by", "owner"]) == 0
    output = capsys.readouterr().out
    assert "Approved example-answer" in output and "publication metadata" in output
    assert main(["approve", "../escape"]) == 1


def test_init_keeps_existing_files(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rules.toml").write_text('name = "mine"\n')
    main(["init"])
    assert (tmp_path / "rules.toml").read_text() == 'name = "mine"\n'
    assert "kept rules.toml" in capsys.readouterr().out


def test_check_fails_with_a_malformed_answer(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GSC_ACCESS_TOKEN", "synthetic-token")
    main(["init"])
    path = tmp_path / "seo-engine.toml"
    path.write_text(path.read_text().replace('site_url = ""', 'site_url = "sc-domain:example.com"'))
    (tmp_path / "answers" / "broken.json").write_text("{broken")
    capsys.readouterr()
    assert main(["check"]) == 1
    output = capsys.readouterr().out
    assert "skipped:" in output and "answers" in output.split("Not ready:")[1]


def test_the_example_config_loads_and_matches_the_defaults():
    from seo_engine.cli import TEMPLATES
    cfg = config.load(TEMPLATES / "seo-engine.example.toml")
    for section in ("opportunities", "draft", "review", "publish", "static", "livecheck", "report"):
        assert cfg[section] == config.DEFAULTS[section], section
    assert cfg["publish"]["mode"] == "dry-run" and cfg["review"]["enabled"] is False


@pytest.mark.parametrize("data,message", [
    ({"publish": {"target": "shopify"}}, "/pages/{slug}"),
    ({"publish": {"target": "git"}}, "turn on [review]"),
    ({"static": {"format": "markdown"}}, "must end in .md"),
    ({"static": {"file": "../outside/{slug}.html"}}, "relative path"),
    ({"review": {"enabled": True, "repo": "not a repo"}}, "owner/name"),
    ({"publish": {"mode": "yolo"}}, "publish.mode"),
    ({"site": {"base_url": "www.example.com"}}, "origin"),
])
def test_config_validation(data, message):
    with pytest.raises(ValueError, match=message.replace("[", r"\[").replace("{", r"\{")):
        config.from_dict(data)


@pytest.mark.parametrize("section,key", [("review", "enabled"), ("review", "auto_merge"), ("draft", "fallbacks")])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, []])
def test_boolean_switches_require_boolean_values(section, key, value):
    with pytest.raises(ValueError, match="boolean"):
        config.from_dict({section: {key: value}})


def test_dotenv_next_to_the_config_is_loaded_without_overriding(tmp_path, monkeypatch):
    from seo_engine.cli import load_env
    monkeypatch.setenv("SEO_ENGINE_TEST_KEEP", "from-shell")
    monkeypatch.delenv("SEO_ENGINE_TEST_NEW", raising=False)
    monkeypatch.delenv("SEO_ENGINE_TEST_EMPTY", raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nSEO_ENGINE_TEST_NEW="quoted value"\nSEO_ENGINE_TEST_KEEP=from-file\n'
                   'SEO_ENGINE_TEST_EMPTY=\nnot a pair\n')
    try:
        assert load_env(env) == ["SEO_ENGINE_TEST_NEW"]
        assert os.environ["SEO_ENGINE_TEST_NEW"] == "quoted value"
        assert os.environ["SEO_ENGINE_TEST_KEEP"] == "from-shell"
    finally:
        os.environ.pop("SEO_ENGINE_TEST_NEW", None)
