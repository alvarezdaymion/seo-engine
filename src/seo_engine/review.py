"""GitHub review: one branch, one commit and one pull request per change, merged only when every
gate passes.

Env:
  GITHUB_TOKEN   a fine-grained token for the one repo: Contents read and write, Pull requests
                 read and write, Checks read

Options in seo-engine.toml ([review]):
  repo             "owner/name"
  base             the branch pull requests target, default "main"
  path             folder inside the repo for the page files, default the repo root
  branch_prefix    every branch the engine creates starts with this, default "seo-engine/"
  auto_merge       merge when every gate passes, default false (a person merges)
  required_checks  check-run names that must succeed before a merge, for example ["build"]
  merge_method     squash (default), merge or rebase

REST calls: read the base branch, create a tree and a commit on top of it, create the branch at
that commit and open the pull request; later read the pull request, its files and its check runs,
and merge with the exact head SHA the gates saw. `_write` refuses anything else before it is
sent: it cannot push to the base branch, move an existing branch, force-push or delete.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from .gates import Gate
from .http import Http, HttpError, Transport, credential
from .state import WriteJournal
from .text import sha256

API = "https://api.github.com"


class GitHubReview:
    def __init__(self, repo: str, base: str = "main", path: str = "", branch_prefix: str = "seo-engine/",
                 merge_method: str = "squash", token: str | None = None, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        token = token or os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise RuntimeError("set GITHUB_TOKEN")
        token = credential(token, "GITHUB_TOKEN")
        self.repo, self.base, self.prefix = repo, base, branch_prefix
        self.path = path.strip("/")
        self.merge_method = merge_method
        self.root = "%s/repos/%s" % (API, repo)
        self.http = Http({"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                          "X-GitHub-Api-Version": "2022-11-28"}, transport)
        self.sleep = sleep
        self.journal = WriteJournal()
        self.before_merge: Callable[[], None] = lambda: None

    def repo_path(self, rel: str) -> str:
        return "%s/%s" % (self.path, rel) if self.path else rel

    def open_pull_request(self, slug: str, files: dict[str, str], title: str, body: str,
                          previous: dict[str, str] | None = None) -> dict[str, Any]:
        key = "base:" + sha256(json.dumps([self.repo, self.base, self.path, slug, files, title, body], sort_keys=True))
        context = self.journal.records.get(key)
        if context is None:
            ref_url = "%s/git/ref/heads/%s" % (self.root, urllib.parse.quote(self.base))
            base_sha = self.http.get(ref_url)["object"]["sha"]
            tree_sha = self.http.get("%s/git/commits/%s" % (self.root, base_sha))["tree"]["sha"]
            snapshot = {p: self.base_file(p, ref=base_sha) for p in files}
            for p, content in snapshot.items():
                if content is not None and content != (previous or {}).get(p):
                    raise PermissionError("refused to replace an unverified repository file: %s" % p)
            context = {"base": base_sha, "tree": tree_sha, "snapshot": snapshot}
            self.journal.records[key] = context
            self.journal.save()
        if not isinstance(context, dict) or not all(k in context for k in ("base", "tree", "snapshot")):
            raise RuntimeError("pull request intent is malformed; reconcile it before continuing")
        base_sha, tree_sha = context["base"], context["tree"]
        entries = [{"path": self.repo_path(p), "mode": "100644", "type": "blob", "content": c}
                   for p, c in sorted(files.items())]
        tree = self._write("POST", self.root + "/git/trees", {"base_tree": tree_sha, "tree": entries})
        commit = self._write("POST", self.root + "/git/commits",
                             {"message": title, "tree": tree["sha"], "parents": [base_sha]})
        branch = "%s%s-%s" % (self.prefix, slug, commit["sha"][:7])
        self._write("POST", self.root + "/git/refs", {"ref": "refs/heads/" + branch, "sha": commit["sha"]})
        pr = self._write("POST", self.root + "/pulls",
                         {"title": title, "head": branch, "base": self.base, "body": body})
        return {"number": int(pr["number"]), "url": pr.get("html_url", ""), "branch": branch, "head": commit["sha"],
                "files": sorted(self.repo_path(p) for p in files), "base_files": context["snapshot"]}

    def pull(self, number: int) -> dict[str, Any]:
        return self.http.get("%s/pulls/%d" % (self.root, number))

    def target_ok(self, pr: dict[str, Any], branch: str | None = None) -> bool:
        base, head = pr.get("base") or {}, pr.get("head") or {}
        return (base.get("ref") == self.base and (base.get("repo") or {}).get("full_name") == self.repo
                and (head.get("repo") or {}).get("full_name") == self.repo
                and str(head.get("ref", "")).startswith(self.prefix)
                and (branch is None or head.get("ref") == branch))

    def gates(self, number: int, head: str, files: list[str], required_checks: list[str],
              base_files: dict[str, str | None] | None = None, branch: str | None = None) -> list[Gate]:
        pr = self.pull(number)
        for _ in range(5):  # GitHub computes mergeability in the background
            if pr.get("mergeable") is not None or pr.get("state") != "open":
                break
            self.sleep(2)
            pr = self.pull(number)
        if not self.target_ok(pr, branch):
            return [Gate("target", False, "pull request repository or target branch changed or is missing")]
        changed = sorted(f["filename"] for f in self.http.get("%s/pulls/%d/files" % (self.root, number),
                                                               {"per_page": 100}) or [])
        out = [
            Gate("target", self.target_ok(pr, branch), "pull request repository or branch changed or is missing"),
            Gate("open", pr.get("state") == "open" and not pr.get("merged"),
                 "pull request is %s" % ("merged" if pr.get("merged") else pr.get("state"))),
            Gate("head", (pr.get("head") or {}).get("sha") == head, "the branch changed after the engine pushed it"),
            Gate("files", changed == sorted(files), "changed files: %s" % ", ".join(changed)),
        ]
        if base_files is not None:
            out.append(Gate("base files", all(self.base_file(p) == c for p, c in base_files.items()),
                            "base files changed since the engine opened the pull request"))
        if required_checks:
            runs = (self.http.get("%s/commits/%s/check-runs" % (self.root, head), {"per_page": 100}) or {})
            items = runs.get("check_runs", [])
            complete = (isinstance(items, list) and len(items) < 100
                        and runs.get("total_count") == len(items))
            done = set()
            for name in required_checks:
                named = [r for r in items if isinstance(r, dict) and r.get("name") == name]
                if not complete or not named or any(
                    type(r.get("id")) is not int or type((r.get("app") or {}).get("id")) is not int
                    or r.get("head_sha") != head for r in named
                ):
                    continue
                if len({r["app"]["id"] for r in named}) != 1:
                    continue
                latest = max(named, key=lambda r: r["id"])
                if latest.get("status") == "completed" and latest.get("conclusion") == "success":
                    done.add(name)
            missing = [c for c in required_checks if c not in done]
            out.append(Gate("checks", not missing, "not passed yet: %s" % ", ".join(missing) if missing else "passed"))
        out.append(Gate("mergeable", pr.get("mergeable") is True and pr.get("mergeable_state") == "clean",
                        "GitHub reports %s" % pr.get("mergeable_state")))
        return out

    def merge(self, number: int, head: str, title: str, branch: str | None = None) -> str:
        if not self.target_ok(self.pull(number), branch):
            raise PermissionError("refused merge: pull request repository or target branch changed or is missing")
        self.before_merge()
        r = self._write("PUT", "%s/pulls/%d/merge" % (self.root, number),
                        {"sha": head, "merge_method": self.merge_method, "commit_title": title})
        if not (r or {}).get("merged"):
            raise RuntimeError("GitHub did not confirm the merge of #%d" % number)
        return str(r.get("sha", ""))

    def base_file(self, rel: str, ref: str | None = None) -> str | None:
        """A file's text on the base branch, or None when it is not there."""
        try:
            r = self.http.get("%s/contents/%s" % (self.root, urllib.parse.quote(self.repo_path(rel))),
                              {"ref": ref or self.base})
        except HttpError as e:
            if e.status == 404:
                return None
            raise
        return base64.b64decode(r.get("content", "")).decode("utf-8", "replace")

    def _write(self, method: str, url: str, body: dict[str, Any]) -> Any:
        """The only write path. Each allowed call is listed; anything else is refused unsent."""
        root, branch = re.escape(self.root), str(body.get("ref") or body.get("head") or "")
        allowed = (
            method == "POST" and url == self.root + "/git/trees" and "base_tree" in body,
            method == "POST" and url == self.root + "/git/commits" and len(body.get("parents", [])) == 1,
            method == "POST" and url == self.root + "/git/refs" and branch.startswith("refs/heads/" + self.prefix)
            and branch != "refs/heads/" + self.base,
            method == "POST" and url == self.root + "/pulls" and body.get("base") == self.base
            and branch.startswith(self.prefix),
            method == "PUT" and re.fullmatch(root + r"/pulls/\d+/merge", url) is not None and bool(body.get("sha"))
            and body.get("merge_method") == self.merge_method,
        )
        if not any(allowed):
            raise PermissionError("refused GitHub write: %s %s" % (method, url))
        key = sha256(json.dumps([method, url, body], sort_keys=True))
        return self.journal.perform(key, lambda: self.http.request(method, url, json_body=body),
                                    lambda: self._reconcile(method, url, body))

    def _reconcile(self, method: str, url: str, body: dict[str, Any]) -> Any:
        if method == "POST" and url == self.root + "/git/refs":
            try:
                ref = self.http.get(self.root + "/git/ref/" + body["ref"][len("refs/"):])
            except HttpError as e:
                if e.status == 404:
                    return None
                raise
            return ref if (ref.get("object") or {}).get("sha") == body["sha"] else None
        if method == "POST" and url == self.root + "/pulls":
            pulls = self.http.get(url, {"state": "all", "head": self.repo.split("/")[0] + ":" + body["head"],
                                        "base": self.base, "per_page": 100})
            if not isinstance(pulls, list) or len(pulls) >= 100:
                return None
            matches = [p for p in pulls if self.target_ok(p) and p["head"].get("ref") == body["head"]]
            return matches[0] if len(matches) == 1 else None
        return None
