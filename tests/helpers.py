import datetime as dt
import json
from pathlib import Path

from seo_engine import config
from seo_engine.answers import load_answer
from seo_engine.search import Row
from seo_engine.text import sha256

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "seo_engine" / "templates"
RULES = TEMPLATES / "rules.example.toml"
TODAY = dt.date(2026, 3, 12)
SITE = "https://www.example.com"

TRANSCRIPT = """Good question. A dripping kitchen faucet usually means the cartridge or a washer inside it has \
worn out. Hard water wears those parts faster than soft water does.

Most faucets can be fixed in under an hour with a replacement cartridge. A faucet that drips once a second wastes \
about 5 percent of the water a home uses. Do not overtighten the handle, because that only crushes the washer.

If the drip comes back within a month, call us and we will look at the valve seat."""


def write_answer(directory: Path, answer_id: str = "ra-100", question: str = "Why is my kitchen faucet dripping?",
                 transcript: str = TRANSCRIPT, approved: bool = True, **meta):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ("%s.txt" % answer_id)).write_text(transcript)
    data = {"id": answer_id, "question": question, "speaker": {"name": "Sam Placeholder", "role": "Plumber"},
            "video_url": "https://videos.example.com/%s.mp4" % answer_id, "recorded_on": "2026-01-05",
            "target_queries": [], "cut": [], "sources": [], **meta}
    approval = {"status": "approved", "by": "owner", "on": "2026-01-06", "transcript_sha256": sha256(transcript)}
    data["approval"] = approval if approved else {"status": "recorded"}
    (directory / ("%s.json" % answer_id)).write_text(json.dumps(data))
    if approved:
        data["approval"]["revision_sha256"] = load_answer(directory / ("%s.json" % answer_id)).revision_sha256
        (directory / ("%s.json" % answer_id)).write_text(json.dumps(data))
    return load_answer(directory / ("%s.json" % answer_id))


def make_cfg(root: Path, **sections):
    data = {
        "site": {"name": "Test Co", "base_url": SITE, "brand_terms": ["test co"]},
        "answers": {"dir": "answers"},
        "claims": {"rules": str(RULES)},
        "publish": {"target": "static", "mode": "live"},
        "livecheck": {"fetch_base": "file", "attempts": 1},
    }
    for key, value in sections.items():
        data[key] = {**data.get(key, {}), **value}
    return config.from_dict(data, root)


def daily(query: str, page: str, impressions: int, clicks: int, position: float, end: dt.date, days: int = 28):
    """Spread a period's totals evenly over `days` daily rows ending at `end`."""
    rows = []
    for i in range(days):
        d = end - dt.timedelta(days=i)
        im = impressions // days + (1 if i < impressions % days else 0)
        cl = clicks // days + (1 if i < clicks % days else 0)
        if im:
            rows.append(Row(d.isoformat(), query, SITE + page, cl, im, position))
    return rows


class ListSource:
    def __init__(self, rows=None, error=None):
        self.rows, self.error = rows or [], error

    def fetch(self, start, end):
        if self.error:
            raise RuntimeError(self.error)
        return [r for r in self.rows if start.isoformat() <= r.date <= end.isoformat()]
