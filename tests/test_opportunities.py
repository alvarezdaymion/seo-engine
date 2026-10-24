import datetime as dt

import pytest
from helpers import SITE, TODAY, daily

from seo_engine import config
from seo_engine.opportunities import expected_ctr, rank
from seo_engine.search import Row, between, complete_weeks, is_brand, periods, total

OCFG = config.DEFAULTS["opportunities"]
END = TODAY - dt.timedelta(days=2)
PREV_END = END - dt.timedelta(days=28)


def ranked(current, previous=(), managed=()):
    return rank(list(current), list(previous), OCFG, ["test co"], set(managed))


def test_expected_ctr_follows_the_curve_and_interpolates():
    assert expected_ctr(1, OCFG) == 0.28
    assert expected_ctr(4.5, OCFG) == pytest.approx(0.07)
    assert expected_ctr(15, OCFG) == 0.01
    assert expected_ctr(40, OCFG) == 0.004


def test_position_is_weighted_by_impressions():
    rows = [Row("2026-03-01", "q", SITE + "/a", 1, 100, 2.0), Row("2026-03-02", "q", SITE + "/a", 1, 300, 6.0)]
    t = total(rows)
    assert t.position == pytest.approx(5.0) and t.ctr == pytest.approx(0.005)


def test_a_query_whose_best_page_ranks_low_has_no_page():
    opps = ranked(daily("kitchen faucet dripping", "/services/", 1000, 3, 18.0, END))
    assert [(o.kind, o.query) for o in opps] == [("no_page", "kitchen faucet dripping")]
    assert opps[0].missed_clicks == pytest.approx(1000 * 0.06 - 3)  # expected CTR at position 5, minus actual


def test_a_query_that_ranks_well_somewhere_has_a_page():
    rows = daily("kitchen faucet dripping", "/services/", 1000, 3, 18.0, END) + \
        daily("kitchen faucet dripping", "/blog/faucets/", 400, 30, 3.0, END)
    assert [o for o in ranked(rows) if o.kind == "no_page"] == []


def test_brand_queries_low_volume_and_managed_pages_are_left_out():
    rows = daily("test co plumbing", "/", 5000, 10, 15.0, END) + daily("rare query", "/", 20, 0, 30.0, END) + \
        daily("water heater popping noise", "/answers/popping/", 900, 1, 15.0, END)
    assert ranked(rows, managed=[SITE + "/answers/popping"]) == []
    assert is_brand("Test Co reviews", ["test co"]) and is_brand("testco hours", ["test co"])


def test_low_ctr_page_in_the_top_ten():
    opps = ranked(daily("furnace filter replacement", "/blog/filters/", 2000, 10, 4.0, END))
    low = [o for o in opps if o.kind == "low_ctr"]
    assert len(low) == 1 and low[0].page == SITE + "/blog/filters/"
    assert low[0].missed_clicks == pytest.approx(2000 * 0.08 - 10)


def test_a_page_at_its_expected_ctr_is_not_low_ctr():
    assert ranked(daily("furnace filter replacement", "/blog/filters/", 2000, 160, 4.0, END)) == []


def test_slipping_page_against_the_previous_period():
    cur = daily("drain cleaning near me", "/services/drains/", 1300, 26, 9.8, END)
    prev = daily("drain cleaning near me", "/services/drains/", 1250, 61, 5.2, PREV_END)
    slip = [o for o in ranked(cur, prev) if o.kind == "slipping"]
    assert len(slip) == 1 and slip[0].prev_position == pytest.approx(5.2)
    assert slip[0].missed_clicks == pytest.approx(1300 * (expected_ctr(5.2, OCFG) - expected_ctr(9.8, OCFG)))


def test_ranking_is_by_missed_clicks():
    rows = daily("small gap", "/a/", 300, 1, 15.0, END) + daily("big gap", "/b/", 3000, 1, 15.0, END)
    assert [o.query for o in ranked(rows)] == ["big gap", "small gap"]


def test_periods_and_complete_weeks():
    cur, prev = periods(dt.date(2026, 3, 10), 28)
    assert cur == (dt.date(2026, 2, 11), dt.date(2026, 3, 10)) and prev[1] == dt.date(2026, 2, 10)
    week, before = complete_weeks(dt.date(2026, 3, 12))  # a Thursday
    assert week == (dt.date(2026, 3, 2), dt.date(2026, 3, 8)) and before == (dt.date(2026, 2, 23), dt.date(2026, 3, 1))
    assert complete_weeks(dt.date(2026, 3, 9))[0][1] == dt.date(2026, 3, 8)  # Monday: yesterday closes the week
    assert len(between(daily("q", "/", 28, 0, 1, END), cur[0], cur[1])) == 28
