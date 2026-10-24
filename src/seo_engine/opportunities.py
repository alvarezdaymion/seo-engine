"""Rank search opportunities by the clicks each one is missing.

Three kinds, all computed from Search Console rows with brand queries removed:

  no_page   A query whose best page ranks below `no_page_position`: nothing on the site answers
            it yet. Missed clicks = impressions x (expected CTR at `target_position` - actual CTR).
  low_ctr   A page in the top `low_ctr_max_position` whose CTR is under `low_ctr_ratio` of the
            expected CTR for its position. Missed = impressions x (expected CTR - actual CTR).
  slipping  A page that lost `slip_positions` or more against the previous period.
            Missed = impressions x (expected CTR at the old position - at the new one).

Expected CTR comes from `ctr_curve`, a rough curve to replace with your own numbers. The output is
an estimate for ranking work, not a forecast.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .search import Row, Totals, group, non_brand

KIND_LABELS = {"no_page": "no page", "low_ctr": "low CTR", "slipping": "slipping"}


@dataclass
class Opportunity:
    kind: str
    query: str
    page: str
    impressions: float
    clicks: float
    position: float
    missed_clicks: float
    prev_position: float | None = None
    managed: bool = False

    @property
    def target(self) -> str:
        return '"%s"' % self.query if self.kind == "no_page" else self.page

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "label": KIND_LABELS[self.kind]}


def norm_url(url: str) -> str:
    return url.split("#")[0].rstrip("/").lower()


def expected_ctr(position: float, ocfg: dict[str, Any]) -> float:
    curve = [float(v) for v in ocfg["ctr_curve"]]
    page_two = float(ocfg["ctr_page_two"])
    if position <= 1:
        return curve[0]
    if position < 11:
        lo = int(position)
        nxt = curve[lo] if lo < 10 else page_two
        return curve[lo - 1] + (nxt - curve[lo - 1]) * (position - lo)
    return page_two if position <= 20 else float(ocfg["ctr_beyond"])


def rank(current: list[Row], previous: list[Row], ocfg: dict[str, Any], brand_terms: list[str],
         managed_urls: set[str]) -> list[Opportunity]:
    cur = non_brand(current, brand_terms)
    prev = non_brand(previous, brand_terms)
    managed = {norm_url(u) for u in managed_urls}
    min_impr = float(ocfg["min_impressions"])
    out: list[Opportunity] = []

    by_query: dict[str, dict[str, Totals]] = {}
    for r in cur:
        by_query.setdefault(r.query, {}).setdefault(r.page, Totals()).add(r)
    for query, pages in by_query.items():
        t = Totals()
        for p in pages.values():
            t.clicks += p.clicks
            t.impressions += p.impressions
        if t.impressions < min_impr or any(norm_url(p) in managed for p in pages):
            continue
        best_page, best = min(pages.items(), key=lambda kv: (kv[1].position, kv[0]))
        if best.position <= float(ocfg["no_page_position"]):
            continue
        missed = t.impressions * (expected_ctr(float(ocfg["target_position"]), ocfg) - t.ctr)
        if missed > 0:
            out.append(Opportunity("no_page", query, best_page, t.impressions, t.clicks, best.position, missed))

    top_query: dict[str, tuple[float, str]] = {}
    for (query, page), t in group_pairs(cur).items():
        if t.impressions > top_query.get(page, (-1.0, ""))[0]:
            top_query[page] = (t.impressions, query)
    prev_pages = group(prev, "page")
    for page, t in group(cur, "page").items():
        if t.impressions < min_impr:
            continue
        query, is_managed = top_query[page][1], norm_url(page) in managed
        expected = expected_ctr(t.position, ocfg)
        if t.position <= float(ocfg["low_ctr_max_position"]) and t.ctr < expected * float(ocfg["low_ctr_ratio"]):
            out.append(Opportunity("low_ctr", query, page, t.impressions, t.clicks, t.position,
                                   t.impressions * (expected - t.ctr), managed=is_managed))
        p = prev_pages.get(page)
        if p and p.impressions >= min_impr and t.position - p.position >= float(ocfg["slip_positions"]):
            missed = t.impressions * (expected_ctr(p.position, ocfg) - expected)
            if missed > 0:
                out.append(Opportunity("slipping", query, page, t.impressions, t.clicks, t.position, missed,
                                       prev_position=p.position, managed=is_managed))
    out.sort(key=lambda o: (-o.missed_clicks, -o.impressions, o.kind, o.query, o.page))
    return out


def group_pairs(rows: list[Row]) -> dict[tuple[str, str], Totals]:
    out: dict[tuple[str, str], Totals] = {}
    for r in rows:
        out.setdefault((r.query, r.page), Totals()).add(r)
    return out
