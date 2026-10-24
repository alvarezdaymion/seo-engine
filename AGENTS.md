# AGENTS.md

seo-engine reads Search Console, ranks opportunities by missed clicks, builds pages only from approved recorded answers, checks every claim and the live page, and ships changes through gated pull requests.

If you were asked to set seo-engine up for someone, follow [SETUP.md](SETUP.md). The rules below are for changing the code.

## Layout

- `src/seo_engine/config.py`: config schema, defaults and validation; `templates/seo-engine.example.toml` mirrors it.
- `search.py`, `searchconsole.py`: daily rows, windows, the CSV source and the Search Console client.
- `opportunities.py`: the ranking. Pure functions over rows.
- `answers.py`: recorded answers and `approval_problems`, the approval gate.
- `planner.py`: the action ladder and the refusals.
- `page.py`, `draft.py`, `claims.py`: building, drafting, claim checks and fixes.
- `gates.py`, `review.py`: release gates and the GitHub flow. `publish/`: static and Shopify publishers.
- `livecheck.py`: the live-page check. `report.py`: the weekly report and webhook.
- `engine.py`: one run. `state.py`: files under `state/`. `cli.py`: commands. `demo.py`: synthetic data and `MemoryGitHub`.

## Rules

- **No page without an approved recording.** `planner.plan`, `page.build_page` and `gates.recording_gate` each enforce it, and `Engine.publish` runs the gates again right before anything goes out. Approval and the pending build must match the complete recording revision, including publication metadata. Older approvals need explicit reapproval. Do not add a code path that creates or rewrites page content from anything else: a keyword list, a model prompt, a template without a transcript. Keep `tests/test_recording_gate.py` passing and extend it when you add a path that creates pages.
- **No quota.** Do not add settings or logic that make the engine produce pages on a schedule. `max_new_pages_per_day` is a ceiling only, and an idle run is a normal result.
- **Only the speaker's words.** Page text comes from the approved transcript minus the `cut` lines. The model selects whole sentences in transcript order; deterministic tidying removes only filler sounds and normalizes whitespace and quotes. It must pass `draft.grounding_problems`; `build_page` checks the answer section against the transcript again. Claim fixes remove sentences, attach approved sources or add disclosures. Nothing rewords a speaker.
- **Gates run right before the action they guard**, every time: reload recordings and rules before every merge and publish, including after connector waits. Static and Shopify recovery also need the previous build's fresh approval and claims gates before restoring visibility; missing or mismatched recovery evidence leaves it unpublished. GitHub gates check the configured repository, base, engine branch and exact head SHA. Required checks need an unambiguous provider and its latest result on that head. Closing a PR holds its pending revision, never a newer approved revision.
- **Writes are allow-listed.** `GitHubReview._write` and `ShopifyPublisher._write` refuse anything outside their list before it is sent. Do not add deletes, force pushes, writes to the base branch, or writes to other Shopify resources.
- **Off by default.** Review, auto-merge, live publishing, report delivery and the model stay off unless the config turns them on. A merge also needs `[publish] mode = "live"`.
- **The engine only edits pages it built.** Opportunities on other pages are reported, never acted on. Publishers refuse existing content without verified ownership; GitHub also checks the base files against the saved build. Static ownership records bind the recording to its site folder, file paths and resolved destinations independently of body integrity, so a corrupt managed article can be repaired without permitting unmanaged collisions. Static writes and rollback refuse symlink paths, use exclusive temporary files and directory handles, and verify the written file's binding after the live check before granting ownership.
- **Uncertain writes are held.** Persist intent before external writes or model requests, keep recovery data before static and Shopify mutations, and never automatically resend an uncertain operation. Reconcile GitHub branches and PRs by reads. Serialize runs and deliveries; interrupted locks require operator recovery. Git deployments require a manual revert when the live check fails.
- **Unknown is not zero.** Missing search data is reported as unavailable. CSV metrics require explicit finite nonnegative numbers; blank cells never confirm zero activity. Fetch both reporting weeks independently of the ranking window, retain source coverage, and refuse truncated pagination. A week without complete final data is never reported as complete.
- **One action per run.** A failing step becomes a blocker and never stops the rest of the run. Isolate saved-build loading failures per page so other checks and the report still run. Report scheduling failures must not discard completed action state or the last-run summary. Report commands keep collection and delivery under the run lock and refuse delivery when initialization or lock acquisition fails.
- **Secrets** come from environment variables. Never commit `.env`, `seo-engine.toml`, `rules.toml`, `answers/`, `state/`, `site/` or `reports/`, and never put tokens or webhook URLs in errors, state files or reports (`http.py` errors omit headers and query strings). Validate webhook URLs before recording delivery intent and sanitize every request exception without its raw message.
- Runtime code is standard library only; the Anthropic SDK is an optional extra.

## Development

- Python 3.11+, `uv`. Run `uv run pytest` and `uv run ruff check` before a PR.
- Add a failing test with each behavior change. Network code is tested with fake transports (`demo.MemoryGitHub` and the fakes in `tests/`); tests never call real APIs.
- Test data is synthetic: example.com and example.org addresses, obviously fake names, no real queries or metrics.
