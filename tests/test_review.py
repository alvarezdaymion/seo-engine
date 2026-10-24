import pytest

from seo_engine.demo import MemoryGitHub
from seo_engine.http import HttpError
from seo_engine.review import GitHubReview

TOKEN = "test-token-not-real"
FILES = {"answers/faucet/index.html": "<html>faucet</html>"}


def make(checks=("build",), **kw):
    fake = MemoryGitHub(checks=checks)
    seen = []

    def transport(method, url, headers, body, timeout):
        seen.append(headers.get("Authorization"))
        return fake(method, url, headers, body, timeout)

    review = GitHubReview("example/site", token=TOKEN, transport=transport, sleep=lambda s: None, **kw)
    return review, fake, seen


def open_one(review):
    return review.open_pull_request("faucet", FILES, "Add answer page: faucet", "body")


def writes(fake):
    return [(m, p) for m, p, _ in fake.calls if m != "GET"]


def test_a_change_is_one_branch_one_commit_one_pull_request():
    review, fake, seen = make(path="content")
    pr = open_one(review)
    assert pr["number"] == 1 and pr["branch"].startswith("seo-engine/faucet-")
    assert pr["files"] == ["content/answers/faucet/index.html"]
    assert writes(fake) == [("POST", "/repos/example/site/git/trees"), ("POST", "/repos/example/site/git/commits"),
                            ("POST", "/repos/example/site/git/refs"), ("POST", "/repos/example/site/pulls")]
    assert fake.refs["main"] == "c0"  # the base branch is untouched until a merge
    assert set(seen) == {"Bearer " + TOKEN}


def test_all_gates_pass_and_the_merge_uses_the_gated_head():
    review, fake, _ = make()
    pr = open_one(review)
    gates = review.gates(pr["number"], pr["head"], pr["files"], ["build"])
    assert [(g.name, g.ok) for g in gates] == [("target", True), ("open", True), ("head", True),
                                               ("files", True), ("checks", True),
                                               ("mergeable", True)]
    review.merge(pr["number"], pr["head"], "Add answer page: faucet (#1)")
    merge = [d for m, p, d in fake.calls if m == "PUT"][0]
    assert merge == {"sha": pr["head"], "merge_method": "squash", "commit_title": "Add answer page: faucet (#1)"}
    assert review.base_file("answers/faucet/index.html") == FILES["answers/faucet/index.html"]
    assert review.base_file("answers/other/index.html") is None


def test_gates_fail_when_the_branch_moved_or_other_files_changed():
    review, fake, _ = make()
    pr = open_one(review)
    fake.push(1, "answers/extra.html", "<p>added by someone else</p>")
    gates = {g.name: g for g in review.gates(1, pr["head"], pr["files"], [])}
    assert not gates["head"].ok and not gates["files"].ok and "answers/extra.html" in gates["files"].detail


def test_gates_fail_on_missing_checks_and_unclean_merge_state():
    review, fake, _ = make(checks=())
    pr = open_one(review)
    fake.pulls[1].update(mergeable=False, mergeable_state="dirty")
    gates = {g.name: g for g in review.gates(1, pr["head"], pr["files"], ["build"])}
    assert not gates["checks"].ok and "build" in gates["checks"].detail
    assert not gates["mergeable"].ok and "dirty" in gates["mergeable"].detail


def test_a_merge_with_a_stale_head_is_refused_by_github():
    review, fake, _ = make()
    pr = open_one(review)
    fake.push(1, "answers/extra.html", "x")
    with pytest.raises(HttpError) as e:
        review.merge(1, pr["head"], "title")
    assert e.value.status == 409 and TOKEN not in str(e.value)


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/git/refs", {"ref": "refs/heads/main", "sha": "x"}),
    ("POST", "/git/refs", {"ref": "refs/heads/feature", "sha": "x"}),
    ("PATCH", "/git/refs/heads/main", {"sha": "x", "force": True}),
    ("DELETE", "/git/refs/heads/seo-engine/faucet", {}),
    ("POST", "/pulls", {"title": "t", "head": "seo-engine/x", "base": "release", "body": ""}),
    ("PUT", "/pulls/1/merge", {"merge_method": "squash"}),
    ("PUT", "/pulls/1/merge", {"sha": "x", "merge_method": "rebase"}),
    ("PUT", "/contents/answers/faucet/index.html", {"message": "m", "content": "eA=="}),
])
def test_any_other_write_is_refused_before_it_is_sent(method, path, body):
    review, fake, _ = make()
    with pytest.raises(PermissionError):
        review._write(method, review.root + path, body)
    assert fake.calls == []


def test_missing_token_fails_clearly(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        GitHubReview("example/site")


@pytest.mark.parametrize("field,value", [
    ("ref", "other"), ("repo", {"full_name": "example/other"}), ("repo", None),
])
def test_merge_refuses_a_changed_or_missing_target(field, value):
    review, fake, _ = make()
    pr = open_one(review)
    fake.pulls[1]["base"][field] = value
    assert any(not g.ok for g in review.gates(1, pr["head"], pr["files"], []))
    with pytest.raises(PermissionError):
        review.merge(1, pr["head"], "title")
    assert not any(m == "PUT" for m, _, _ in fake.calls)


@pytest.mark.parametrize("case", ["providers", "latest", "head", "missing", "truncated"])
def test_required_checks_fail_closed_on_ambiguous_or_stale_results(case):
    review, fake, _ = make()
    pr = open_one(review)
    good = {"id": 100, "name": "build", "app": {"id": 10}, "head_sha": pr["head"],
            "status": "completed", "conclusion": "success"}
    runs = [good]
    if case == "providers":
        runs.append({**good, "id": 101, "app": {"id": 20}, "conclusion": "failure"})
    elif case == "latest":
        runs.append({**good, "id": 101, "conclusion": "failure"})
    elif case == "head":
        good["head_sha"] = "other-head"
    elif case == "missing":
        good.pop("app")
    original = fake.route

    def route(method, path, data, query):
        if path.endswith("/check-runs"):
            return 200, {"total_count": len(runs) + (1 if case == "truncated" else 0), "check_runs": runs}
        return original(method, path, data, query)

    fake.route = route
    gates = {g.name: g for g in review.gates(1, pr["head"], pr["files"], ["build"])}
    assert not gates["checks"].ok


def test_merge_requires_the_recorded_head_branch():
    review, fake, _ = make()
    pr = open_one(review)
    fake.pulls[1]["head"]["ref"] = "seo-engine/other"
    gates = review.gates(1, pr["head"], pr["files"], [], branch=pr["branch"])
    assert any(not g.ok for g in gates)
    with pytest.raises(PermissionError):
        review.merge(1, pr["head"], "title", branch=pr["branch"])
    assert not any(m == "PUT" for m, _, _ in fake.calls)
