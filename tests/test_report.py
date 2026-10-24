import datetime as dt

from helpers import TODAY, daily, make_cfg

from seo_engine.engine import Deps, Engine
from seo_engine.report import build
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
