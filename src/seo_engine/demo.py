"""The demo: a fictional home-services company, synthetic Search Console rows, two synthetic
recorded answers, and an in-memory stand-in for GitHub. No keys and no network: the live check
runs against a server on 127.0.0.1, or against the written file when a port cannot be opened.
"""

from __future__ import annotations

import base64
import datetime as dt
import functools
import hashlib
import http.server
import json
import shutil
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from . import config as config_mod
from .engine import Deps, Engine
from .livecheck import fetch_any
from .review import GitHubReview
from .search import Row

TEMPLATES = Path(__file__).parent / "templates"
DEMO_TODAY = dt.date(2026, 3, 12)
MARKER = ".seo-engine-demo"
SITE = "https://www.example.com"

# query, page, impressions (previous 28 days, last 28 days), clicks (previous, last), position (previous, last)
SERIES = [
    ("example home services", "/", (600, 620), (182, 190), (1.3, 1.2)),
    ("water heater popping noise", "/services/water-heaters/", (1010, 1150), (4, 4), (17.8, 17.2)),
    ("water heater making popping sound", "/services/water-heaters/", (280, 310), (1, 1), (19.9, 19.4)),
    ("tankless water heater installation cost", "/services/water-heaters/", (760, 820), (3, 3), (22.6, 22.1)),
    ("how to relight a pilot light", "/services/furnace-repair/", (900, 980), (2, 2), (25.0, 24.5)),
    ("furnace filter replacement", "/blog/furnace-filter-guide/", (2300, 2410), (21, 19), (4.3, 4.1)),
    ("drain cleaning near me", "/services/drain-cleaning/", (1250, 1300), (61, 22), (5.2, 9.8)),
    ("winterize outdoor faucet", "/blog/winterize-outdoor-faucets/", (380, 400), (16, 18), (6.4, 6.0)),
    ("outdoor faucet cover", "/blog/winterize-outdoor-faucets/", (30, 35), (0, 1), (8.5, 8.0)),
]


def _spread(total: int, weights: list[float]) -> list[int]:
    """Whole numbers per day that add up to exactly `total`."""
    out, acc, done = [], 0.0, 0
    s = sum(weights)
    for w in weights:
        acc += total * w / s
        out.append(round(acc) - done)
        done += out[-1]
    return out


def demo_rows(today: dt.date = DEMO_TODAY, days: int = 28) -> list[Row]:
    """Final data through two days ago: weekdays busier than weekends, slow growth in each period."""
    latest = today - dt.timedelta(days=2)
    rows = []
    for period in (0, 1):
        start = latest - dt.timedelta(days=(2 - period) * days - 1)
        dates = [start + dt.timedelta(days=i) for i in range(days)]
        weights = [(1.1 if d.weekday() < 5 else 0.75) * (1 + 0.01 * (i - days / 2)) for i, d in enumerate(dates)]
        for query, page, impressions, clicks, position in SERIES:
            imps, clks = _spread(impressions[period], weights), _spread(clicks[period], weights)
            for d, im, cl in zip(dates, imps, clks, strict=True):
                if im:
                    rows.append(Row(d.isoformat(), query, SITE + page, cl, im, position[period]))
    return rows


class ListSource:
    name = "demo"

    def __init__(self, rows: list[Row]):
        self.rows = rows

    def fetch(self, start: dt.date, end: dt.date) -> list[Row]:
        return [r for r in self.rows if start.isoformat() <= r.date <= end.isoformat()]


class MemoryGitHub:
    """Enough of the GitHub REST API, in memory, for the review flow: refs, trees, commits,
    pull requests, check runs, merge and contents. A transport for `GitHubReview`."""

    def __init__(self, repo: str = "example/site", checks: tuple[str, ...] = ("build",)):
        self.prefix = "/repos/%s" % repo
        self.repo = repo
        self.trees: dict[str, dict[str, str]] = {"t0": {}}
        self.commits: dict[str, dict[str, Any]] = {"c0": {"tree": "t0", "parents": []}}
        self.refs = {"main": "c0"}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.checks = checks
        self.calls: list[tuple[str, str, Any]] = []

    def _id(self, data: Any) -> str:
        return hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def push(self, number: int, path: str, content: str) -> str:
        """Someone else pushes a commit to a pull request's branch (used by tests)."""
        pr = self.pulls[number]
        files = {**self.trees[self.commits[pr["head"]["sha"]]["tree"]], path: content}
        tid = self._id(files)
        self.trees[tid] = files
        cid = self._id({"tree": tid, "parents": [pr["head"]["sha"]]})
        self.commits[cid] = {"tree": tid, "parents": [pr["head"]["sha"]]}
        self.refs[pr["head"]["ref"]] = pr["head"]["sha"] = cid
        return cid

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None,
                 timeout: float) -> tuple[int, bytes]:
        parts = urllib.parse.urlsplit(url)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        data = json.loads(body) if body else None
        self.calls.append((method, path, data))
        if not path.startswith(self.prefix):
            return 404, b'{"message": "Not Found"}'
        status, out = self.route(method, path[len(self.prefix):], data, query)
        return status, json.dumps(out).encode()

    def route(self, method: str, path: str, data: Any, query: dict[str, list[str]]) -> tuple[int, Any]:
        seg = path.strip("/").split("/")
        if method == "GET" and path.startswith("/git/ref/heads/"):
            name = path[len("/git/ref/heads/"):]
            return (200, {"object": {"sha": self.refs[name]}}) if name in self.refs else (404, {"message": "Not Found"})
        if method == "GET" and seg[:2] == ["git", "commits"]:
            return 200, {"sha": seg[2], "tree": {"sha": self.commits[seg[2]]["tree"]}}
        if method == "POST" and path == "/git/trees":
            files = dict(self.trees[data["base_tree"]])
            files.update({e["path"]: e["content"] for e in data["tree"]})
            tid = self._id(files)
            self.trees[tid] = files
            return 201, {"sha": tid}
        if method == "POST" and path == "/git/commits":
            cid = self._id(data)
            self.commits[cid] = {"tree": data["tree"], "parents": data["parents"]}
            return 201, {"sha": cid}
        if method == "POST" and path == "/git/refs":
            name = data["ref"][len("refs/heads/"):]
            if name in self.refs:
                return 422, {"message": "Reference already exists"}
            self.refs[name] = data["sha"]
            return 201, {"ref": data["ref"], "object": {"sha": data["sha"]}}
        if method == "POST" and path == "/pulls":
            n = len(self.pulls) + 1
            self.pulls[n] = {"number": n, "state": "open", "merged": False, "title": data["title"],
                             "head": {"ref": data["head"], "sha": self.refs[data["head"]]},
                             "base": {"ref": data["base"]},
                             "mergeable": True, "mergeable_state": "clean",
                             "html_url": "memory://%s/pull/%d" % (self.repo, n)}
            return 201, self.pulls[n]
        if seg[0] == "pulls" and len(seg) >= 2 and int(seg[1]) in self.pulls:
            pr = self.pulls[int(seg[1])]
            if method == "GET" and len(seg) == 2:
                return 200, pr
            if method == "GET" and seg[2:] == ["files"]:
                base = self.trees[self.commits[self.refs[pr["base"]["ref"]]]["tree"]]
                head = self.trees[self.commits[pr["head"]["sha"]]["tree"]]
                return 200, [{"filename": p} for p in sorted(head) if base.get(p) != head[p]]
            if method == "PUT" and seg[2:] == ["merge"]:
                if data.get("sha") != pr["head"]["sha"]:
                    return 409, {"message": "Head branch was modified"}
                base_ref = pr["base"]["ref"]
                cid = self._id({"tree": self.commits[pr["head"]["sha"]]["tree"], "parent": self.refs[base_ref]})
                self.commits[cid] = {"tree": self.commits[pr["head"]["sha"]]["tree"], "parents": [self.refs[base_ref]]}
                self.refs[base_ref] = cid
                pr.update(state="closed", merged=True)
                return 200, {"merged": True, "sha": cid}
        if method == "GET" and seg[0] == "commits" and seg[2:] == ["check-runs"]:
            runs = [{"name": c, "status": "completed", "conclusion": "success"} for c in self.checks]
            return 200, {"check_runs": runs}
        if method == "GET" and seg[0] == "contents":
            ref = query.get("ref", ["main"])[0]
            files = self.trees[self.commits[self.refs[ref]]["tree"]]
            name = urllib.parse.unquote("/".join(seg[1:]))
            if name not in files:
                return 404, {"message": "Not Found"}
            return 200, {"content": base64.b64encode(files[name].encode()).decode()}
        return 404, {"message": "Not Found"}


def demo_config(root: Path, fetch_base: str = "file") -> config_mod.Config:
    return config_mod.from_dict({
        "site": {"name": "Example Home Services", "base_url": SITE, "page_path": "/answers/{slug}/",
                 "brand_terms": ["example home services", "example home"]},
        "answers": {"dir": str(TEMPLATES / "demo" / "answers")},
        "claims": {"rules": str(TEMPLATES / "rules.example.toml")},
        "review": {"enabled": True, "repo": "example/site", "auto_merge": True, "required_checks": ["build"]},
        "publish": {"target": "static", "mode": "live"},
        "livecheck": {"fetch_base": fetch_base, "attempts": 1},
    }, root)


def prepare_dir(root: Path) -> None:
    """Use an empty folder, or one an earlier demo created. Never clears anything else."""
    if root.exists() and any(root.iterdir()) and not (root / MARKER).exists():
        raise RuntimeError("%s is not empty and was not made by the demo; pick another --dir" % root)
    for sub in ("state", "site", "reports"):
        shutil.rmtree(root / sub, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("Created by `seo-engine demo`. Safe to delete.\n")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


def start_server(directory: Path, port: int = 0) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(_Quiet, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_demo(root: Path, port: int = 0) -> tuple[dict[str, Any], config_mod.Config, Any]:
    """Run the engine once on the demo world. Returns the run summary, the config and the server."""
    root = Path(root).resolve()
    prepare_dir(root)
    (root / "site").mkdir(exist_ok=True)
    try:
        server = start_server(root / "site", port)
        fetch_base = "http://127.0.0.1:%d" % server.server_address[1]
    except OSError:
        server, fetch_base = None, "file"
    cfg = demo_config(root, fetch_base)
    review = GitHubReview("example/site", token="demo-token-not-real", transport=MemoryGitHub(),
                          sleep=lambda s: None)
    deps = Deps(search=ListSource(demo_rows()), review=review, fetch=fetch_any, sleep=lambda s: None)
    result = Engine(cfg, deps, today=DEMO_TODAY).run(report="force")
    return result, cfg, server
