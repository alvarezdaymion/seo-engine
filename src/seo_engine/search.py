"""Search rows and the arithmetic on them.

Every source returns daily rows of (date, query, page, clicks, impressions, position), where
position is that day's average. Totals over a window weight position by impressions, the same
way Search Console does. A query with no rows is unobserved, never zero demand.
"""

from __future__ import annotations

import csv
import datetime as dt
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .text import words


@dataclass(frozen=True)
class Row:
    date: str
    query: str
    page: str
    clicks: float
    impressions: float
    position: float


class SearchRows(list[Row]):
    """Final rows with dates the source confirms; dates absent from coverage remain unknown."""

    def __init__(self, rows: Iterable[Row], covered_dates: Iterable[str] | None = None):
        super().__init__(rows)
        self.covered_dates = frozenset(covered_dates if covered_dates is not None else (r.date for r in self))


@dataclass
class Totals:
    clicks: float = 0.0
    impressions: float = 0.0
    _weighted: float = 0.0

    def add(self, row: Row) -> None:
        self.clicks += row.clicks
        self.impressions += row.impressions
        self._weighted += row.position * row.impressions

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0

    @property
    def position(self) -> float:
        return self._weighted / self.impressions if self.impressions else 0.0


def between(rows: Iterable[Row], start: dt.date, end: dt.date) -> list[Row]:
    lo, hi = start.isoformat(), end.isoformat()
    return [r for r in rows if lo <= r.date <= hi]


def total(rows: Iterable[Row]) -> Totals:
    t = Totals()
    for r in rows:
        t.add(r)
    return t


def group(rows: Iterable[Row], key: str) -> dict[str, Totals]:
    out: dict[str, Totals] = {}
    for r in rows:
        out.setdefault(getattr(r, key), Totals()).add(r)
    return out


def is_brand(query: str, brand_terms: list[str]) -> bool:
    q = " ".join(words(query))
    compact = q.replace(" ", "")
    for term in brand_terms:
        t = " ".join(words(term))
        if t and ((" %s " % t) in (" %s " % q) or t.replace(" ", "") in compact):
            return True
    return False


def non_brand(rows: Iterable[Row], brand_terms: list[str]) -> list[Row]:
    return [r for r in rows if not is_brand(r.query, brand_terms)]


def latest_date(rows: Iterable[Row]) -> dt.date | None:
    dates = [r.date for r in rows]
    dates.extend(getattr(rows, "covered_dates", ()))
    return dt.date.fromisoformat(max(dates)) if dates else None


def periods(latest: dt.date, days: int) -> tuple[tuple[dt.date, dt.date], tuple[dt.date, dt.date]]:
    """The last `days` days ending at the latest final date, and the `days` before them."""
    cur = (latest - dt.timedelta(days=days - 1), latest)
    prev_end = cur[0] - dt.timedelta(days=1)
    return cur, (prev_end - dt.timedelta(days=days - 1), prev_end)


def complete_weeks(today: dt.date) -> tuple[tuple[dt.date, dt.date], tuple[dt.date, dt.date]]:
    """The last complete Monday-Sunday week before `today`, and the week before it."""
    end = today - dt.timedelta(days=today.weekday() + 1)
    start = end - dt.timedelta(days=6)
    return (start, end), (start - dt.timedelta(days=7), start - dt.timedelta(days=1))


class CsvSource:
    """Daily rows from a CSV with columns date,query,page,clicks,impressions,position.

    Fits a Search Console bulk export or any script that writes the same columns.
    """

    name = "csv"

    def __init__(self, path: Path):
        self.path = Path(path)

    def fetch(self, start: dt.date, end: dt.date) -> list[Row]:
        if not self.path.exists():
            raise RuntimeError("search CSV not found: %s" % self.path)
        rows = []
        with self.path.open(newline="") as f:
            reader = csv.DictReader(f)
            for rec in reader:
                metrics = []
                for key in ("clicks", "impressions", "position"):
                    try:
                        number = float(rec.get(key))
                        if not math.isfinite(number) or number < 0:
                            raise ValueError("invalid metric")
                    except (TypeError, ValueError):
                        raise ValueError("search CSV row %d: %s needs an explicit finite nonnegative number"
                                         % (reader.line_num, key)) from None
                    metrics.append(number)
                rows.append(Row(rec["date"], rec["query"], rec["page"], *metrics))
        return SearchRows(between(rows, start, end))
