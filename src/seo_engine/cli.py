"""Command line: seo-engine demo | init | check | approve | run | report."""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import os
import shutil
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from . import config as config_mod
from .answers import approval_problems, approve, load_answers
from .claims import load_rules
from .engine import Engine
from .text import is_slug

TEMPLATES = Path(__file__).parent / "templates"


def rel(path: str | Path) -> str:
    try:
        r = os.path.relpath(path)
    except ValueError:
        return str(path)
    return str(path) if r.startswith("..") else r


def _num(v: float) -> str:
    return format(int(round(v)), ",")


def _where(url: str) -> str:
    return url if url.startswith(("http://", "https://")) else rel(url)


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _gates(gates: list[dict[str, Any]]) -> str:
    return ", ".join("%s %s" % (g["name"], "ok" if g["ok"] else "FAILED (%s)" % g["detail"]) for g in gates)


def summary(r: dict[str, Any]) -> str:
    out: list[str] = []
    s = r.get("search") or {}
    if s.get("error"):
        out.append("Search data: unavailable (%s)" % s["error"])
    elif s.get("latest"):
        out.append("Search data: %s daily rows, final through %s. Last %d days (%s to %s) against the %d before."
                   % (_num(s["rows"]), s["latest"], s["days"], s["current"][0], s["current"][1], s["days"]))
    else:
        out.append("Search data: no rows in the window yet.")
    plan = r.get("plan") or {}
    if plan.get("lines"):
        out.append("\nPlan, in rank order (clicks missed per %d days, estimated):" % s.get("days", 28))
        for i, ln in enumerate(plan["lines"], 1):
            target = ln["target"]
            if target.startswith(("http://", "https://")):
                target = urllib.parse.urlsplit(target).path or "/"
            target = target if len(target) <= 44 else target[:41] + "..."
            nums = ("%7s impr  pos %4.1f  ~%-4s" % (_num(ln["impressions"]), ln["position"], _num(ln["missed_clicks"]))
                    if ln["impressions"] else "%28s" % "")
            verdict = ln["verdict"].upper() if ln["chosen"] else ln["verdict"]
            out.append("  %2d. %-9s %-44s %s %s: %s" % (i, ln["kind"], target, nums, verdict, ln["detail"]))
    if plan.get("note"):
        out.append("  " + plan["note"])
    for g in r.get("live_gate") or []:
        state = "passing" if g["ok"] else "FAILING: " + "; ".join(g["problems"])
        out.append("Live check %s: %s" % (_where(g["url"]), state))
    out += _action(r.get("action"))
    rep = r.get("report")
    if rep:
        out.append("\nWeekly report: %s%s" % (rel(rep["path"]), " (sent)" if rep.get("sent") else ""))
        if rep.get("complete"):
            out.append("  Week of %s to %s: %s non-brand clicks (week before: %s), %s impressions (week before: %s)."
                       % (rep["week"][0], rep["week"][1], _num(rep["clicks"][0]), _num(rep["clicks"][1]),
                          _num(rep["impressions"][0]), _num(rep["impressions"][1])))
        else:
            out.append("  Week of %s to %s: final data not complete yet, so no search numbers." % tuple(rep["week"]))
        out.append("  Questions to record: %d. Recordings waiting for approval: %d."
                   % (rep["questions_to_record"], rep["awaiting_approval"]))
    out.append("\nResult: %s" % r["status"])
    if r["blockers"]:
        out.append("Blockers:")
        out += ["  - " + b for b in r["blockers"]]
    return "\n".join(out)


def _action(a: dict[str, Any] | None) -> list[str]:
    if not a:
        return []
    out = [""]
    if a["kind"] == "build":
        out.append("Build: %s from recording %s, answered by %s (approved %s)"
                   % (a["slug"], a["recording"], a["speaker"], a["approved_on"] or "?"))
        out.append("  Evidence: %s" % a["evidence"])
        out.append("  Draft: %s, %s%s. Left out %s cut from the approved edit."
                   % (a["draft"], _plural(a["answer_sentences"], "answer sentence"),
                      " (%s)" % a["draft_note"] if a["draft_note"] else "", _plural(a["cut_sentences"], "sentence")))
    elif a["kind"] == "snippet":
        out.append("Snippet: %s (top query \"%s\")" % (a.get("url"), a.get("query")))
        d = a.get("description")
        if d:
            out.append('  Description: "%s"\n            -> "%s"' % (d["before"], d["after"]))
    else:
        out.append("%s: %s" % (a["kind"].capitalize(), a.get("url") or a["slug"]))
        if a.get("problems"):
            out.append("  Live check problems: %s" % "; ".join(a["problems"]))
    c = a.get("claims")
    if c:
        first = "passed" if not c["first"] else _plural(len(c["first"]), "finding")
        out.append("  Claim check (%s): %s" % (c["rules"], first))
        out += ['    %s: "%s"' % (f["kind"], f["text"] or f["reason"]) for f in c["first"]]
        out += ["  Fix: %s" % f for f in c["fixes"]]
        if c["first"]:
            left = "passed" if not c["final"] else _plural(len(c["final"]), "finding") + " left"
            out.append("  Claim check again: %s" % left)
    if a.get("gates"):
        out.append("  Gates: %s" % _gates(a["gates"]))
    rv = a.get("review")
    if rv:
        out.append("Review: pull request #%s %s, branch %s"
                   % (rv.get("number"), rv.get("url", ""), rv.get("branch", "")))
        if a.get("merge_gates"):
            out.append("  Merge gates: %s" % _gates(a["merge_gates"]))
        out.append("  %s" % {"merged": "Merged.", "waiting": "Not merged: waiting for the gates above.",
                             "opened": "Opened."}.get(rv["status"], rv["status"]))
    p = a.get("publish")
    if p:
        where = rel(p["where"]) if p.get("where") else ", ".join(p["files"])
        out.append("Publish: %s %s" % (p["status"], where))
    lv = a.get("live")
    if lv:
        out.append("Live check %s: %s" % (_where(lv["url"]), "passed (%s)" % ", ".join(lv["passed"]) if lv["ok"]
                                          else "FAILED: " + "; ".join(lv["problems"])))
    if a.get("rollback"):
        out.append("Rollback: %s" % a["rollback"])
    if a.get("draft_file"):
        out.append("Draft page: %s" % rel(a["draft_file"]))
    return out


def load_env(path: Path) -> list[str]:
    """Read KEY=value lines from a .env file into the environment. Variables already set win."""
    loaded = []
    if not path.is_file():
        return loaded
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        value = value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"" else value
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load_config(path: str) -> config_mod.Config:
    """The config, after loading the .env file that sits next to it."""
    load_env(Path(path).resolve().parent / ".env")
    return config_mod.load(path)


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    result, cfg, server = run_demo(Path(args.dir), port=args.port if args.serve else 0)
    print("seo-engine demo: Example Home Services, a fictional company. Synthetic data and an in-memory GitHub;"
          " no keys, no network.\n")
    print(summary(result))
    a = result.get("action") or {}
    if args.serve and server:
        path = cfg["site"]["page_path"].format(slug=a.get("slug", ""))
        print("\nServing the demo site at http://127.0.0.1:%d%s (Ctrl-C to stop)" % (server.server_address[1], path),
              flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    elif a.get("slug"):
        page = cfg.path(cfg["static"]["dir"]) / cfg["static"]["file"].format(slug=a["slug"])
        print("\nOpen %s in a browser, or run: seo-engine demo --serve" % rel(page))
    if server:
        server.shutdown()
    return 0 if result["status"] == "published" else 1


def cmd_init(args: argparse.Namespace) -> int:
    targets = [(TEMPLATES / "seo-engine.example.toml", Path(args.config)),
               (TEMPLATES / "rules.example.toml", Path(args.config).parent / "rules.toml"),
               (TEMPLATES / "answer.example.json", Path(args.config).parent / "answers" / "example-answer.json"),
               (TEMPLATES / "answer.example.txt", Path(args.config).parent / "answers" / "example-answer.txt")]
    for src, dest in targets:
        if dest.exists() and not args.force:
            print("kept %s (exists; --force overwrites)" % dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        print("wrote %s" % dest)
    print("Next: edit seo-engine.toml and rules.toml, add recorded answers to answers/, then run: seo-engine check")
    return 0


def _env(*names: str) -> tuple[bool, str]:
    missing = [n for n in names if not os.environ.get(n)]
    return not missing, ("missing env: " + ", ".join(missing)) if missing else "env set"


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    problems: list[str] = []
    print("Config OK: %s" % rel(Path(args.config).resolve()))
    print("Site: %s, pages at %s" % (cfg["site"]["name"], cfg["site"]["base_url"] + cfg["site"]["page_path"]))
    s = cfg["search"]
    if s["source"] == "csv":
        ok, state = cfg.path(s["csv"]).is_file(), "file found" if cfg.path(s["csv"]).is_file() else "file missing"
    else:
        creds = os.environ.get("GSC_SERVICE_ACCOUNT_FILE") or os.environ.get("GSC_ACCESS_TOKEN")
        ok = bool(creds) and bool(s["site_url"])
        state = "env set" if ok else "needs site_url and GSC_SERVICE_ACCOUNT_FILE (or GSC_ACCESS_TOKEN)"
    where = rel(cfg.path(s["csv"])) if s["source"] == "csv" else s["site_url"]
    print("Search: %s %s: %s" % (s["source"], where, state))
    if not ok:
        problems.append("search")
    answers, errors = load_answers(cfg.path(cfg["answers"]["dir"]))
    print("Recorded answers in %s: %d" % (rel(cfg.path(cfg["answers"]["dir"])), len(answers)))
    for a in answers:
        why = approval_problems(a)
        print('  %-16s %-34s "%s"' % (a.id, "approved" if not why else "; ".join(why)[:34], a.question[:60]))
    for e in errors:
        print("  skipped: %s" % e)
    if errors:
        problems.append("answers")
    try:
        pack = load_rules(cfg.path(cfg["claims"]["rules"]))
        counts = "%s, %s" % (_plural(len(pack.rules), "rule"), _plural(len(pack.disclosures), "disclosure"))
        print('Rule pack %s: "%s", %s' % (rel(cfg.path(cfg["claims"]["rules"])), pack.name, counts))
    except Exception as e:
        print("Rule pack: %s" % str(e)[:200])
        problems.append("rules")
    d = cfg["draft"]
    if d["mode"] == "model":
        ok = importlib.util.find_spec("anthropic") is not None
        print("Draft: model %s, %s" % (d["model"], "anthropic installed" if ok else "run: uv sync --extra llm"))
        if not ok:
            problems.append("draft")
    else:
        print("Draft: template")
    rc = cfg["review"]
    if rc["enabled"]:
        ok, state = _env("GITHUB_TOKEN")
        print("Review: GitHub %s into %s, auto-merge %s, required checks %s: %s"
              % (rc["repo"], rc["base"], "on" if rc["auto_merge"] else "off", rc["required_checks"] or "none", state))
        if not ok:
            problems.append("review")
    else:
        print("Review: off (changes are published without a pull request)")
    p = cfg["publish"]
    if p["target"] == "shopify":
        ok, state = _env("SHOPIFY_STORE", "SHOPIFY_ADMIN_TOKEN")
        if not ok and p["mode"] == "live":
            problems.append("publish")
    elif p["target"] == "git":
        state = "%s files committed to %s through review" % (cfg["static"]["format"], rc["repo"] or "the review repo")
    else:
        state = "%s files in %s" % (cfg["static"]["format"], rel(cfg.path(cfg["static"]["dir"])))
    print("Publish: %s, %s, at most %s a day: %s"
          % (p["target"], p["mode"], _plural(int(p["max_new_pages_per_day"]), "new page"), state))
    rp = cfg["report"]
    if rp["send"] == "webhook":
        ok, state = _env("SEO_ENGINE_REPORT_WEBHOOK")
        if not ok:
            problems.append("report")
    else:
        state = "file only"
    print("Report: %s, %s" % (rp["weekday"], state))
    print("Ready." if not problems else "Not ready: fix %s." % ", ".join(problems))
    return 0 if not problems else 1


def cmd_approve(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if not is_slug(args.id):
        print("answer ids are lowercase letters, digits and dashes")
        return 1
    path = cfg.path(cfg["answers"]["dir"]) / ("%s.json" % args.id)
    if not path.exists():
        print("no recorded answer at %s" % rel(path))
        return 1
    answer = approve(path, args.by, dt.date.today())
    left = approval_problems(answer)
    print("Approved %s, bound to recording revision sha256 %s..." % (answer.id, answer.revision_sha256[:12]))
    if left:
        print("It still cannot back a page: %s" % "; ".join(left))
        return 1
    print("Editing the transcript or publication metadata from now on withdraws this approval.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    result = Engine(load_config(args.config)).run()
    if not args.quiet:
        print(summary(result))
    return 1 if result["blockers"] else 0


def cmd_report(args: argparse.Namespace) -> int:
    try:
        engine = Engine(load_config(args.config))
    except (RuntimeError, OSError, ValueError) as e:
        print("Report unavailable: %s" % str(e)[:200])
        return 1
    result = engine.run(execute=False, report="force", send_report=args.send)
    meta = result.get("report")
    if meta:
        print(meta["text"])
        print("Written to %s%s" % (rel(meta["path"]), ", and sent" if meta["sent"] else ""))
    for blocker in result["blockers"]:
        print("Report blocker: " + blocker)
    return 1 if result["blockers"] else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="seo-engine", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run once on synthetic data: no keys, no network")
    d.add_argument("--dir", default="seo-engine-demo", help="where the demo writes state, pages and the report")
    d.add_argument("--serve", action="store_true", help="keep serving the demo site on localhost")
    d.add_argument("--port", type=int, default=8766)
    d.set_defaults(fn=cmd_demo)
    i = sub.add_parser("init", help="write seo-engine.toml, rules.toml and an example answer")
    i.add_argument("--config", default="seo-engine.toml")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=cmd_init)
    c = sub.add_parser("check", help="validate config, rule pack and recorded answers; show which credentials are set")
    c.add_argument("--config", default="seo-engine.toml")
    c.set_defaults(fn=cmd_check)
    a = sub.add_parser("approve", help="approve a recorded answer's transcript and publication metadata")
    a.add_argument("id")
    a.add_argument("--by", default="owner", help="who approved it, kept in the answer file")
    a.add_argument("--config", default="seo-engine.toml")
    a.set_defaults(fn=cmd_approve)
    r = sub.add_parser("run", help="one run: search data, live checks, one planned action, the weekly report when due")
    r.add_argument("--config", default="seo-engine.toml")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(fn=cmd_run)
    w = sub.add_parser("report", help="write the weekly report now (changes nothing)")
    w.add_argument("--config", default="seo-engine.toml")
    w.add_argument("--send", action="store_true", help="also post it to SEO_ENGINE_REPORT_WEBHOOK")
    w.set_defaults(fn=cmd_report)
    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
