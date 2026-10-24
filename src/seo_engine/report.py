"""The weekly report: what search did last week, what the engine did, and what it needs from you.

Search numbers cover the last complete Monday-Sunday week against the week before, with brand
queries excluded. Both weeks need source coverage for every day; missing coverage is unavailable,
never zero. The report also lists the questions that have
search demand but no approved recorded answer: record one, approve it, and the engine builds the
page.

Delivery: always a Markdown file in reports/. With [report] send = "webhook" the same text is
posted as JSON, {"text": "..."} by default (the key is `payload_key`), to SEO_ENGINE_REPORT_WEBHOOK,
which must be an https URL such as a Slack incoming webhook.
"""

from __future__ import annotations

import datetime as dt
import os
import urllib.parse
from typing import Any

from .answers import RecordedAnswer, approval_problems
from .http import Http, HttpError, Transport
from .opportunities import KIND_LABELS, Opportunity
from .planner import Plan
from .search import Row, between, complete_weeks, group, non_brand, total


def _n(v: float) -> str:
    return format(int(round(v)), ",")


def _count(v: float, word: str) -> str:
    return "%s %s%s" % (_n(v), word, "" if int(round(v)) == 1 else "s")


def _change(cur: float, prev: float, fmt: str = "%+d") -> str:
    return fmt % (cur - prev)


def build(cfg: Any, rows: list[Row], latest: dt.date | None, today: dt.date, records: dict[str, dict[str, Any]],
          events: list[dict[str, Any]], plan: Plan | None, opportunities: list[Opportunity],
          answers: list[RecordedAnswer], search_error: str = "",
          coverage: set[str] | None = None) -> tuple[str, dict[str, Any]]:
    week, prev_week = complete_weeks(today)
    coverage = coverage if coverage is not None else set(getattr(rows, "covered_dates", (r.date for r in rows)))
    if coverage and latest is None:
        latest = dt.date.fromisoformat(max(coverage))
    meta: dict[str, Any] = {"week": [week[0].isoformat(), week[1].isoformat()], "complete": False}
    out = ["# Weekly search report: %s" % cfg["site"]["name"], "",
           "Week of %s to %s, compared with %s to %s. Brand queries are excluded."
           % (week[0], week[1], prev_week[0], prev_week[1]), "", "## Search", ""]
    if search_error:
        out.append("Search data is unavailable: %s" % search_error)
    elif latest is None or latest < week[1]:
        out.append("Not available yet: final Search Console data runs through %s and the week ends %s. "
                   "A partial week is never reported." % (latest or "no data", week[1]))
    elif not all((prev_week[0] + dt.timedelta(days=i)).isoformat() in coverage for i in range(14)):
        out.append("Search data is unavailable for one or more days in the reporting weeks. "
                   "Missing days are never treated as zero.")
    else:
        nb = non_brand(rows, cfg["site"]["brand_terms"])
        c, p = total(between(nb, *week)), total(between(nb, *prev_week))
        meta.update(complete=True, clicks=[c.clicks, p.clicks], impressions=[c.impressions, p.impressions])
        out += ["| | This week | Week before | Change |", "|---|---:|---:|---:|",
                "| Clicks | %s | %s | %s |" % (_n(c.clicks), _n(p.clicks), _change(c.clicks, p.clicks)),
                "| Impressions | %s | %s | %s |" % (_n(c.impressions), _n(p.impressions),
                                                    _change(c.impressions, p.impressions)),
                "| CTR | %.1f%% | %.1f%% | %+.1f pts |" % (c.ctr * 100, p.ctr * 100, (c.ctr - p.ctr) * 100),
                "| Average position | %.1f | %.1f | %+.1f |" % (c.position, p.position, c.position - p.position),
                "", "Top queries this week:", ""]
        queries = sorted(group(between(nb, *week), "query").items(),
                         key=lambda kv: (-kv[1].clicks, -kv[1].impressions, kv[0]))
        for q, t in queries[:int(cfg["report"]["top_queries"])]:
            out.append('- "%s": %s, %s, position %.1f' % (q, _count(t.clicks, "click"),
                                                         _count(t.impressions, "impression"), t.position))

    out += ["", "## Pages the engine manages", ""]
    live = [r for r in records.values() if r.get("live")]
    failing = [r for r in live if not (r.get("last_check") or {}).get("ok")]
    out.append("%s live, %d passing the live check." % (_count(len(live), "page"), len(live) - len(failing)))
    for r in failing:
        out.append("- failing: %s (%s)" % (r.get("url"), "; ".join((r.get("last_check") or {}).get("problems", []))))
    since = (today - dt.timedelta(days=7)).isoformat()
    recent = [e for e in events if e.get("event") == "published" and str(e.get("day", "")) >= since]
    if recent:
        out.append("Published or updated in the last 7 days:")
        out += ["- %s (%s)" % (e.get("url", ""), "new page" if e.get("new") else "update") for e in recent]

    verdicts = {ln.target: ln for ln in (plan.lines if plan else [])}
    out += ["", "## Top opportunities", ""]
    for i, o in enumerate(opportunities[:5], 1):
        ln = verdicts.get(o.target)
        verdict = " %s: %s." % (ln.verdict.capitalize(), ln.detail) if ln else ""
        out.append("%d. %s %s: %s, position %.1f, about %s missed.%s"
                   % (i, KIND_LABELS[o.kind], o.target, _count(o.impressions, "impression"), o.position,
                      _count(o.missed_clicks, "click"), verdict))
    if not opportunities:
        out.append("None found in the current data.")

    if plan and plan.blockers:
        out += ["", "## Page ownership blockers", ""]
        out += ["- " + problem for problem in plan.blockers]

    refused = plan.refused if plan else []
    meta["questions_to_record"] = len(refused)
    out += ["", "## Questions to record", "",
            "Search demand with no approved recorded answer. Record an answer and approve it, and the engine "
            "builds the page. It never writes one without a recording.", ""]
    out += ['- %s: %s, position %.1f (%s)' % (ln.target, _count(ln.impressions, "impression"), ln.position, ln.detail)
            for ln in refused] or ["- none"]

    waiting = [a for a in answers if not a.approved]
    meta["awaiting_approval"] = len(waiting)
    out += ["", "## Recordings waiting for approval", ""]
    out += ['- %s "%s": %s' % (a.id, a.question, "; ".join(approval_problems(a))) for a in waiting] or ["- none"]

    held = [(s, r) for s, r in sorted(records.items())
            if r.get("status") in ("in_review", "merged", "held", "closed", "rolled_back")]
    out += ["", "## Waiting or held", ""]
    out += ["- %s: %s%s" % (r.get("url") or s, r["status"], " (%s)" % r["reason"] if r.get("reason") else "")
            for s, r in held] or ["- none"]
    return "\n".join(out) + "\n", meta


def webhook_url(url: str | None = None) -> str:
    url = url or os.environ.get("SEO_ENGINE_REPORT_WEBHOOK", "")
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme == "https" and bool(parsed.hostname) and parsed.port != 0
                 and not parsed.username and not parsed.password and not parsed.fragment
                 and not any(not 33 <= ord(c) <= 126 for c in url))
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("set SEO_ENGINE_REPORT_WEBHOOK to a valid https URL")
    return url


def send_webhook(text: str, payload_key: str = "text", url: str | None = None,
                 transport: Transport | None = None) -> None:
    url = webhook_url(url)
    try:
        Http(transport=transport).request("POST", url, json_body={payload_key: text})
    except HttpError as e:  # the webhook URL is a secret, so it never goes into the message
        if e.status:
            raise RuntimeError("the report webhook returned HTTP %d" % e.status) from None
        raise RuntimeError("the report webhook request failed") from None
    except Exception as e:
        raise RuntimeError("the report webhook request failed (%s)" % type(e).__name__) from None
