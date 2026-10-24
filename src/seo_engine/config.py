"""Load seo-engine.toml and fill in defaults. Every threshold the engine uses lives here."""

from __future__ import annotations

import copy
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

DEFAULTS: dict[str, Any] = {
    "site": {
        "name": "Your business",
        "base_url": "https://www.example.com",
        "page_path": "/answers/{slug}/",
        "brand_terms": [],
    },
    "search": {"source": "searchconsole", "site_url": "", "days": 28, "country": "", "csv": ""},
    "opportunities": {
        "min_impressions": 50,
        "no_page_position": 10.0,     # a query whose best page ranks below this has no page that answers it
        "target_position": 5.0,       # where a new page could reasonably rank, used to size "no page" upside
        "low_ctr_max_position": 10.0,
        "low_ctr_ratio": 0.5,         # low CTR = below half the expected CTR for the position
        "slip_positions": 3.0,        # lost this many positions against the previous period
        # Rough organic CTR by position 1-10, then positions 11-20 and 21+. Replace with your own.
        "ctr_curve": [0.28, 0.16, 0.11, 0.08, 0.06, 0.045, 0.035, 0.03, 0.025, 0.02],
        "ctr_page_two": 0.01,
        "ctr_beyond": 0.004,
    },
    "answers": {"dir": "answers", "match_threshold": 0.6, "answer_sentences": 3},
    "draft": {"mode": "template", "model": "claude-opus-5-5", "effort": "low", "max_tokens": 4000,
              "fallbacks": True},
    "claims": {"rules": "rules.toml"},
    "review": {
        "enabled": False,
        "repo": "",
        "base": "main",
        "path": "",
        "branch_prefix": "seo-engine/",
        "auto_merge": False,
        "required_checks": [],
        "merge_method": "squash",
    },
    "publish": {"target": "static", "mode": "dry-run", "max_new_pages_per_day": 1},
    "static": {"dir": "site", "format": "html", "file": "answers/{slug}/index.html"},
    "shopify": {"api_version": "2026-07"},
    "livecheck": {"fetch_base": "", "attempts": 3, "wait_seconds": 10.0},
    "report": {"weekday": "thursday", "send": "off", "payload_key": "text", "top_queries": 5},
    "paths": {"state": "state", "reports": "reports"},
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class Config:
    data: dict[str, Any]
    root: Path

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def path(self, value: str) -> Path:
        return (self.root / value).resolve()

    @property
    def state_dir(self) -> Path:
        return self.path(self.data["paths"]["state"])

    @property
    def reports_dir(self) -> Path:
        return self.path(self.data["paths"]["reports"])

    def page_url(self, slug: str) -> str:
        return self.data["site"]["base_url"] + self.data["site"]["page_path"].format(slug=slug)


def validate(data: dict[str, Any]) -> None:
    for section, key in (("review", "enabled"), ("review", "auto_merge"), ("draft", "fallbacks")):
        if type(data[section][key]) is not bool:
            raise ValueError("%s.%s must be a boolean" % (section, key))
    checks = data["review"]["required_checks"]
    if not isinstance(checks, list) or any(not isinstance(c, str) or not c.strip() for c in checks):
        raise ValueError("review.required_checks must be a list of check names")
    site = data["site"]
    site["base_url"] = str(site["base_url"]).rstrip("/")
    if not re.match(r"^https?://[^/\s]+$", site["base_url"]):
        raise ValueError("site.base_url must be an origin like https://www.example.com")
    if not site["page_path"].startswith("/") or "{slug}" not in site["page_path"]:
        raise ValueError("site.page_path must start with / and contain {slug}")
    choices = {
        ("search", "source"): ("searchconsole", "csv"),
        ("draft", "mode"): ("template", "model"),
        ("publish", "target"): ("static", "shopify", "git"),
        ("publish", "mode"): ("dry-run", "live"),
        ("static", "format"): ("html", "markdown"),
        ("review", "merge_method"): ("squash", "merge", "rebase"),
        ("report", "weekday"): WEEKDAYS,
        ("report", "send"): ("off", "webhook"),
    }
    for (section, key), allowed in choices.items():
        if data[section][key] not in allowed:
            raise ValueError("%s.%s must be one of: %s" % (section, key, ", ".join(allowed)))
    static = data["static"]
    if "{slug}" not in static["file"] or static["file"].startswith("/") or ".." in static["file"]:
        raise ValueError("static.file must be a relative path that contains {slug}")
    if not static["file"].endswith(".md" if static["format"] == "markdown" else ".html"):
        raise ValueError("static.file must end in .md for markdown and .html for html")
    if data["publish"]["target"] == "shopify" and site["page_path"] != "/pages/{slug}":
        raise ValueError('Shopify serves pages at /pages/<handle>: set site.page_path = "/pages/{slug}"')
    if data["publish"]["target"] == "git" and not data["review"]["enabled"]:
        raise ValueError('publish.target = "git" publishes by merging a pull request: turn on [review]')
    if data["review"]["enabled"] and not re.match(r"^[\w.-]+/[\w.-]+$", data["review"]["repo"]):
        raise ValueError('review.repo must be "owner/name" when review is enabled')
    if not data["review"]["branch_prefix"] or data["review"]["branch_prefix"] == data["review"]["base"]:
        raise ValueError("review.branch_prefix must be set and differ from review.base")
    curve = data["opportunities"]["ctr_curve"]
    if len(curve) != 10 or not all(0 < float(v) < 1 for v in curve):
        raise ValueError("opportunities.ctr_curve needs 10 rates between 0 and 1 (positions 1-10)")
    if not 0 < float(data["answers"]["match_threshold"]) <= 1:
        raise ValueError("answers.match_threshold must be between 0 and 1")
    if int(data["publish"]["max_new_pages_per_day"]) < 0:
        raise ValueError("publish.max_new_pages_per_day cannot be negative")


def from_dict(data: dict[str, Any], root: Path | str = ".") -> Config:
    merged = _merge(DEFAULTS, data)
    validate(merged)
    return Config(merged, Path(root).resolve())


def load(path: Path | str) -> Config:
    p = Path(path)
    with p.open("rb") as f:
        data = tomllib.load(f)
    return from_dict(data, p.parent)
