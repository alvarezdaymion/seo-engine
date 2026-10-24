import datetime as dt
from http.client import InvalidURL

import pytest
from helpers import TODAY, ListSource, daily, make_cfg

from seo_engine.engine import Deps, Engine
from seo_engine.report import build, send_webhook
from seo_engine.search import CsvSource


def test_csv_missing_days_do_not_become_a_complete_comparison(tmp_path):
    path = tmp_path / "rows.csv"
    rows = daily("synthetic question", "/", 140, 14, 5, dt.date(2026, 3, 8), days=14)
    rows.pop(4)
    path.write_text("date,query,page,clicks,impressions,position\n" + "".join(
        "%s,%s,%s,%s,%s,%s\n" % (r.date, r.query, r.page, r.clicks, r.impressions, r.position) for r in rows))
    data = CsvSource(path).fetch(dt.date(2026, 2, 1), TODAY)
    _, meta = build(make_cfg(tmp_path), data, dt.date(2026, 3, 8), TODAY, {}, [], None, [], [])
    assert not meta["complete"] and "clicks" not in meta


def test_explicit_final_coverage_can_confirm_empty_weeks(tmp_path):
    coverage = {(dt.date(2026, 2, 23) + dt.timedelta(days=i)).isoformat() for i in range(14)}
    _, meta = build(make_cfg(tmp_path), [], None, TODAY, {}, [], None, [], [], coverage=coverage)
    assert meta["complete"] and meta["clicks"] == [0, 0]


def test_engine_preserves_source_confirmation_of_empty_dates(tmp_path):
    class EmptyFinalRows(list):
        covered_dates = {(dt.date(2026, 2, 23) + dt.timedelta(days=i)).isoformat() for i in range(14)}

    class EmptySource:
        def fetch(self, start, end):
            return EmptyFinalRows()

    result = Engine(make_cfg(tmp_path), Deps(search=EmptySource()), today=TODAY).run(report="force")
    assert result["report"]["complete"] and result["report"]["clicks"] == [0, 0]


@pytest.mark.parametrize("value", ["", " ", None, "nan", "inf", "-1"])
@pytest.mark.parametrize("field", ["clicks", "impressions", "position"])
def test_csv_metrics_must_be_explicit_finite_numbers(tmp_path, field, value):
    values = {"clicks": "0", "impressions": "0", "position": "0", field: value}
    path = tmp_path / "rows.csv"
    path.write_text("date,query,page,clicks,impressions,position\n"
                    "2026-03-08,synthetic question,https://www.example.com/,%s,%s,%s\n"
                    % tuple(values[k] if values[k] is not None else "" for k in ("clicks", "impressions", "position")))
    with pytest.raises(ValueError, match=field):
        CsvSource(path).fetch(dt.date(2026, 2, 23), TODAY)


def test_blank_csv_metrics_are_unavailable_in_the_weekly_report(tmp_path):
    path = tmp_path / "rows.csv"
    path.write_text("date,query,page,clicks,impressions,position\n" + "".join(
        "%s,synthetic question,https://www.example.com/,,,\n" % (dt.date(2026, 2, 23) + dt.timedelta(days=i))
        for i in range(14)))
    cfg = make_cfg(tmp_path, search={"source": "csv", "csv": "rows.csv"})
    result = Engine(cfg, today=TODAY).run(execute=False, report="force")
    assert result["blockers"] and not result["report"]["complete"]
    assert "clicks" not in result["report"]


def test_explicit_zero_csv_metrics_confirm_empty_dates(tmp_path):
    path = tmp_path / "rows.csv"
    path.write_text("date,query,page,clicks,impressions,position\n" + "".join(
        "%s,synthetic question,https://www.example.com/,0,0,0\n" % (dt.date(2026, 2, 23) + dt.timedelta(days=i))
        for i in range(14)))
    cfg = make_cfg(tmp_path, search={"source": "csv", "csv": "rows.csv"})
    result = Engine(cfg, today=TODAY).run(execute=False, report="force")
    assert not result["blockers"] and result["report"]["complete"]
    assert result["report"]["clicks"] == [0, 0]


@pytest.mark.parametrize("error", [InvalidURL, ValueError, RuntimeError])
def test_webhook_request_errors_never_expose_the_url(tmp_path, monkeypatch, error):
    url = "https://hooks.example.com/synthetic-secret"
    monkeypatch.setenv("SEO_ENGINE_REPORT_WEBHOOK", url)

    def fail(*args):
        raise error(url)

    cfg = make_cfg(tmp_path, report={"send": "webhook"})
    for _ in range(2):
        result = Engine(cfg, Deps(search=ListSource(), webhook=fail), today=TODAY).run(report="force")
        assert result["blockers"] and "synthetic-secret" not in str(result)
        assert "synthetic-secret" not in (cfg.state_dir / "last-run.json").read_text()


@pytest.mark.parametrize("url", [
    "https://hooks.example.com/synthetic-secret bad", "https://hooks.example.com/synthetic-secret\n",
    "https://", "https://[invalid/synthetic-secret", "https://hooks.example.com:bad/synthetic-secret",
    "https://hooks.example.com/synthetic-secreté",
])
def test_malformed_webhook_urls_are_refused_without_transport_calls(url):
    calls = []

    def transport(*args):
        calls.append(args)
        return 200, b"ok"

    with pytest.raises(RuntimeError) as caught:
        send_webhook("report", url=url, transport=transport)
    assert calls == [] and "synthetic-secret" not in str(caught.value)


def test_invalid_webhook_encoding_does_not_consume_delivery_intent(tmp_path, monkeypatch):
    monkeypatch.setenv("SEO_ENGINE_REPORT_WEBHOOK", "https://hooks.example.com/synthetic-secreté")
    cfg = make_cfg(tmp_path, report={"send": "webhook"})
    calls = []
    deps = Deps(search=ListSource(), webhook=lambda *args: calls.append(args))
    result = Engine(cfg, deps, today=TODAY).run(execute=False, report="force")
    assert result["blockers"] and "synthetic-secret" not in str(result)
    assert calls == [] and not (cfg.state_dir / "report-deliveries.json").exists()
