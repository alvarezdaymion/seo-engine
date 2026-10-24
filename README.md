# seo-engine

seo-engine does the daily search work a small online business never has time for. It reads Google Search Console, ranks what to do next by the clicks each opportunity is missing, and builds a page only from an approved recorded answer: a customer question that someone on your team answered on video, published with the transcript, the video and the speaker's name. Every claim on the page is checked against your rule pack. With review on, each change is a GitHub pull request that merges only when every gate passes. The live page is checked after publishing, and a weekly report says what moved and which questions to record next.

Project page: [daymionalvarez.com/portfolio/seo-engine](https://daymionalvarez.com/portfolio/seo-engine)

## Why recordings, not a quota

The first version of this system worked as software and failed as marketing. It had a daily article quota, so a model wrote an article a day, and almost nobody clicked them: no real person on the page, topics picked to fill the slot, answers shaped to pass a review instead of answering the question. The fix is a rule in code, not a prompt: **a page can only exist if it is built from an approved recorded answer.** It is enforced in three places:

1. **The planner** refuses a search query that no approved recording covers. The query goes into the weekly report as a question to record.
2. **The page builder** raises `NoApprovedRecording` without one, and accepts only whole transcript sentences in their original order, with deterministic filler cleanup.
3. **The release gates** reload approval and rules right before every merge and publish. Approval binds the transcript and publication metadata, including cuts, sources, question, speaker and video. Any change needs reapproval, and a pending build from an older revision stays blocked even after reapproval.

There is no setting that makes the engine produce pages. `max_new_pages_per_day` is a ceiling, and an idle run is normal. `tests/test_recording_gate.py` pins all of this.

## Quickstart

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/alvarezdaymion/seo-engine.git
cd seo-engine
uv run seo-engine demo            # synthetic data, an in-memory GitHub, no keys, no network
uv run seo-engine demo --serve    # the same, then serves the demo site on http://127.0.0.1:8766
```

To use it on your site, run `uv run seo-engine init`, edit `seo-engine.toml` and `rules.toml`, add recorded answers to `answers/`, put credentials in `.env` (start from `.env.example`; the CLI reads `.env` next to `seo-engine.toml`, and variables already set in the environment win), then `uv run seo-engine check` and `uv run seo-engine run`. Or hand the repo to your coding agent and say "set up seo-engine for my site": [SETUP.md](SETUP.md) is written for it.

## What you see

The demo is a fictional home-services company with 56 days of synthetic Search Console rows and two recorded answers, one approved and one not. Abridged:

```
Plan, in rank order (clicks missed per 28 days, estimated):
   1. low CTR   /blog/furnace-filter-guide/              2,410 impr  pos  4.1  ~169  skip: not built by seo-engine, so reported and never edited
   2. no page   "water heater popping noise"             1,150 impr  pos 17.2  ~65   BUILD: from recording ra-001 (Alex Example)
   3. no page   "how to relight a pilot light"             980 impr  pos 24.5  ~57   refused: no recorded answer covers it
   4. slipping  /services/drain-cleaning/                1,300 impr  pos  9.8  ~47   skip: not built by seo-engine, so reported and never edited
   5. no page   "tankless water heater installation cost"  820 impr  pos 22.1  ~46   refused: recording ra-002 is not approved: status is "recorded"
   6. no page   "water heater making popping sound"        310 impr  pos 19.4  ~18   covered: same recording as a line above (ra-001)

Build: why-is-my-water-heater-making-a-popping-noise from recording ra-001, answered by Alex Example (approved 2026-03-02)
  Draft: template, 3 answer sentences. Left out 1 sentence cut from the approved edit.
  Claim check (Example rule pack): 1 finding
    needs_source: "A thick layer of sediment can make the burner run about 10 percent longer to heat the same water."
  Fix: cited approved source [1] https://www.example.org/water-heater-maintenance for "A thick layer of sediment can make the burner run about..."
  Claim check again: passed
Review: pull request #1 memory://example/site/pull/1, branch seo-engine/why-is-my-water-heater-making-a-popping-noise-ab5a837
  Merge gates: recording ok, claims ok, open ok, head ok, files ok, checks ok, mergeable ok, auto-merge ok, publish mode ok
  Merged.
Publish: created seo-engine-demo/site/answers/why-is-my-water-heater-making-a-popping-noise/index.html
Live check http://127.0.0.1:<port>/answers/why-is-my-water-heater-making-a-popping-noise/: passed (200, no redirect, indexable,
  title, canonical, description, schema, video embed, speaker, source hash)

Weekly report: seo-engine-demo/reports/2026-03-02.md
  Week of 2026-03-02 to 2026-03-08: 18 non-brand clicks (week before: 19), 2,007 impressions (week before: 1,873).
  Questions to record: 2. Recordings waiting for approval: 1.
```

1. **Opportunities**, ranked by estimated clicks missed: a query with no page that ranks for it, a page with low CTR for its position, a page that slipped against the previous period.
2. **The plan.** One action per run. The top line is a page the engine did not build, so it is reported and left alone. Two queries are refused: one has no recording, one has a recording nobody approved. The approved recording gets built.
3. **The page**: the recorded question as the H1, the speaker's name and role, a short direct answer, the video, the full transcript without the line the approved edit cut (a "guaranteed to last twenty years" promise), cited sources and the disclosure from the rule pack.
4. **The claim check** finds a percentage with no source, cites the source that was approved with the recording, and checks again.
5. **Review, merge, publish, live check**: a pull request on the in-memory GitHub, merged after every gate passes, written to the site folder, then fetched from a local server and checked.
6. **The weekly report** in `seo-engine-demo/reports/`, including the questions to record.

## How it runs

`seo-engine run` is one run. Schedule it once a day (cron, systemd and launchd examples are in [examples/scheduling](examples/scheduling)).

1. **Search data.** Daily query and page rows of final Search Console data. The last 28 days are compared with the 28 before. Brand queries (`brand_terms`) are left out.
2. **Live gate.** Every page the engine published is fetched and checked. A failing page is the first thing to repair.
3. **Plan** (`planner.py`), first match wins:
   1. finish a pull request from an earlier run: merge it when every gate passes, publish it once merged (one pull request is open at a time);
   2. repair a live page that failed its check by republishing the approved build;
   3. build a page from an approved recording: first the ones that cover the highest-ranked "no page" queries, then approved recordings that have no page yet;
   4. give a low-CTR page the engine built a new meta description, taken from the transcript sentence that best matches its top query (at most once per page every 28 days);
   5. nothing. An idle run is normal.
4. **Build and check.** The page is built from the recording, the claim check runs, safe fixes are applied and the check runs again. A page with findings is held and listed in the report.
5. **Review, merge and publish**, each behind its own switch (below). After a publish the page is fetched and checked again, with retries for a CDN or deploy. Static and Shopify changes are rolled back if it still fails. For Git deployments, the merge deploys immediately and recovery requires a manual revert.
6. **Report.** On the report weekday (Thursday by default, when last week's final data is usually in), the weekly report is written to `reports/` and sent if delivery is on.

A failing step is recorded as a blocker and never stops the steps after it. A missing or corrupt saved page build blocks its publication while other live checks and the weekly report continue; the damaged build is preserved for recovery. An unreadable or malformed report checkpoint blocks scheduled reporting, while completed actions and the last-run summary are still saved. `seo-engine run` exits 1 when the run has a blocker, so a scheduler can alert on it. Runs and report commands hold `state/run.lock` through collection and delivery; an interrupted run can leave a lock that must be reconciled before another run. `seo-engine report --send` refuses delivery when initialization or lock acquisition fails and exits 1 on blockers.

## Recorded answers

Each recording is two files in `answers/`: `<id>.json` and the transcript `<id>.txt`.

```json
{
  "id": "ra-001",
  "question": "Why is my water heater making a popping noise?",
  "speaker": {"name": "Alex Example", "role": "Lead technician"},
  "video_url": "https://videos.example.com/answers/ra-001.mp4",
  "thumbnail_url": "https://videos.example.com/answers/ra-001.jpg",
  "recorded_on": "2026-02-24",
  "target_queries": ["water heater popping noise"],
  "cut": ["Honestly, if you flush it every year, that tank is guaranteed to last twenty years."],
  "sources": [{"supports": "10 percent longer", "title": "Water heater maintenance basics (example source)",
               "url": "https://www.example.org/water-heater-maintenance"}],
  "approval": {"status": "approved", "by": "owner", "on": "2026-03-02", "transcript_sha256": "82e5f0bf...",
               "revision_sha256": "computed by the approve command"}
}
```

- `cut` lists what the approved video edit removed. Those sentences never reach the page, even though the raw transcript still has them.
- `sources` are links a person approved along with the recording. `supports` is the wording in the transcript each one backs.
- `seo-engine approve <id> --by <name>` writes transcript and recording-revision hashes. Editing the transcript or approved metadata afterwards withdraws approval. Approvals created without a revision hash need reapproval with this command.
- YouTube and Vimeo links become an iframe embed; any other https URL becomes a `<video>` element.
- A query matches a recording when it is one of the `target_queries`, or when its topic words overlap the question's enough (`match_threshold`, a Dice score, 0.6 by default).

The direct answer at the top of the page is the first three transcript sentences that have five words or more. With `[draft] mode = "model"`, a model picks whole sentences instead, in transcript order. Only filler sounds such as "um" and "uh" may be removed; words, punctuation, numbers and qualifiers otherwise stay intact. Any shortened, joined or rearranged sentence rejects the whole draft and the template is used. Model attempts are recorded and reused for the same recording revision and draft settings, including when a failed request fell back to the template. The claim check runs either way.

## The claim check

A rule pack is a TOML file; `seo-engine init` copies [the example](src/seo_engine/templates/rules.example.toml).

| Rule | Example | Fix |
|---|---|---|
| `[[banned]]` phrases | "guaranteed", "best in town", "#1" | Remove the sentence. A speaker is never reworded. |
| `[[needs_source]]` phrases or regex | a percentage, "studies show", "required by law" | Cite a source approved with the recording whose `supports` words are in the sentence, or remove the sentence |
| `[[disclosures]]` | "This answer is general information..." | Add it |

Phrases match whole words and ignore case. The check includes the description, byline, source titles and disclosures. A banned phrase in a field that cannot be safely removed, such as the question or speaker role, holds the page. Body findings remain actionable when the same claim appears in the description. Removing an answer sentence also rebuilds its description; descriptions retain their source sentences so a truncated description can inherit an approved citation without exempting altered wording.

## Switches and gates

| Switch | Default | When on |
|---|---|---|
| `[review] enabled` | `false` | Every change is a branch and pull request on GitHub |
| `[review] auto_merge` | `false` | The engine merges its own pull request when every gate passes |
| `[publish] mode` | `"dry-run"` | `"live"` lets the engine merge and publish |
| `[report] send` | `"off"` | `"webhook"` posts the weekly report |
| `[draft] mode` | `"template"` | `"model"` calls the model to pick the answer sentences |

In dry-run the engine still builds and checks the page and writes it to `state/drafts/<slug>.html`, so you can read exactly what it would publish.

**Before a merge**, every one of these must pass: `recording` (approved, same recording revision the page was built from), `claims` (no findings), `target` (configured repository, base and engine branch), `open`, `head` (nobody pushed to the branch since the engine did), `files` (exactly the page files), `base files` (unchanged since review opened), `checks`, `mergeable` (GitHub reports `clean`), `auto-merge` and `publish mode`. Each required check name must identify one provider, whose latest run succeeded on the exact head. Missing provider information, ambiguous names and incomplete results block merging. The merge sends the head SHA the gates saw. When a person merges instead, the engine publishes only if the merged files are exactly the build it approved. The `recording` and `claims` gates run once more right before any publish, with or without review. For the Git target, a human merge itself deploys, regardless of the engine's dry-run setting; keep those PRs open until go-live.

Each PR identifies its recording revision and approval date. Closing it holds that pending build. A newer approved revision can receive a replacement PR, including when its changes are metadata that leave the rendered page unchanged.

**After a publish**, the live check fetches the page and requires: HTTP 200 with no redirect, no `noindex` or `none` robots directive (header or meta tag), the question in the title, exactly one canonical link pointing at the page, the expected meta description, JSON-LD with an `Article` by the speaker and a `VideoObject` for the video, the video embed, the speaker's name, and article text that still hashes to its `data-seo-engine-source` marker. A failed check or checker exception rolls back static and Shopify changes. A Git deployment that fails its check is reported for a manual revert; the engine cannot undo a merge.

Static and Shopify rollback restore visibility only when the saved previous build matches the recovery snapshot and passes freshly loaded recording and claims gates. Otherwise static recovery removes the verified file written by the run, and Shopify keeps the restored content hidden, including after approval withdrawal or a rule change. Static writes use exclusive temporary files and directory handles; page files and their directories inside the site folder cannot be symlinks. A changed file or directory binding during the live check holds the publication for reconciliation without touching the substituted path. Static publishing requires filesystem support for directory-relative operations and no-follow opens.

## Connectors

| Connector | Status | What it does |
|---|---|---|
| Google Search Console | Real, read-only | Search Analytics API: daily query and page rows of final data, paginated. Service account (signed with the `openssl` command) or an access token |
| CSV | Real | The same rows from a file: `date,query,page,clicks,impressions,position` ([example](examples/search-console.csv)) |
| GitHub | Real | REST API: tree, commit, branch and pull request, then the gates and a merge (squash by default) with the gated head SHA |
| Static files | Real | HTML pages, or Markdown with front matter, written into a folder. Rollback restores the previous file |
| Shopify Pages | Real, Admin REST API | Creates or updates a page and its SEO title and description. Changes stay hidden until all fields succeed. Never deletes; rollback hides or restores. Preview rendering needs no Shopify credentials |
| Git deploy | Real | `[publish] target = "git"`: the reviewed repo is the site, so the merge publishes and the engine waits for the live check |
| Webhook | Real | Posts the weekly report as JSON to an https URL (Slack-style `text`, or `content`) |
| Anthropic | Optional | Picks the answer sentences when `[draft] mode = "model"` (`uv sync --extra llm`) |
| Demo | Synthetic | Search rows, recordings and an in-memory GitHub for `seo-engine demo` |

Tests use fake transports for Search Console, GitHub, Shopify, the webhook and the model; no test calls a real API.

Reports require confirmed dates for both reporting weeks, even with a short ranking window. CSV coverage includes only dates represented by valid rows. Clicks, impressions and position each require an explicit finite nonnegative number; blank, missing or invalid metrics make the data unavailable. A row containing numeric zeros can confirm an empty date. Missing dates and truncated Search Console pagination are unavailable data.

What it does not do: generate images (pages use the video thumbnail), set up redirects or merge overlapping pages, check Google's index status (no URL Inspection API), publish Shopify blog articles (pages only), or send email. It never edits a page it did not build. Search Console leaves out anonymized and low-volume queries, so report totals cover the rows it returns.

## Safety rules

- **No page without an approved recording**, checked by the planner, the builder and the release gates.
- **Nothing public by default.** Review, auto-merge, live publishing, report delivery and the model are all off until you turn them on.
- **Writes are allow-listed per connector** and refused before they are sent: GitHub can only create a tree, a commit, a branch under `branch_prefix`, a pull request into `base`, and a merge with an exact head SHA; Shopify can only create a page, update a page and update one metafield value. Neither can delete or force anything.
- **A speaker is never reworded.** Fixes remove sentences or attach approved sources.
- **Unknown is not zero.** A week without complete final data is reported as not available yet, never as a partial week.
- **Existing content needs ownership evidence.** Static pages are tied to their recording, site folder, file paths and resolved destinations in saved state, so body corruption can be repaired independently of its source marker. Initial static collisions and existing Shopify pages require a valid source marker for the same recording; repository files must match the saved engine build before replacement. Changing the static destination or retargeting a symlink does not transfer ownership. Without a saved ownership record, a static page whose marker is already damaged requires reconciliation.
- **Uncertain writes never repeat automatically.** Intent and recovery snapshots are saved before writes. Lost GitHub branch or PR responses are reconciled by reads; unresolved writes and webhook deliveries stay held. Forced report sends also honor the delivery journal. Corrupt state blocks writes and is preserved for recovery; see [SETUP.md](SETUP.md#recovering-interrupted-operations).
- **Secrets come from environment variables.** Tokens must be nonempty ASCII without whitespace or control characters; malformed values are rejected before use, rather than trimmed. Service-account keys retain their PEM line breaks. HTTP errors expose only a safe failure message and, when available, the status code; URLs, headers, bodies, provider error text and raw exceptions are omitted. Signing and model client failures follow the same rule, and model rejection notes omit returned text and references. Webhook URLs must be ASCII https URLs and are validated before delivery intent is saved.

## Configuration

Everything lives in `seo-engine.toml` (start from `uv run seo-engine init`; defaults are in `src/seo_engine/config.py`):

- `[site]`: name, public origin, page path (`/pages/{slug}` on Shopify) and brand terms.
- `[search]`: Search Console property or CSV file, window length, optional country.
- `[opportunities]`: thresholds and the CTR-by-position curve used to estimate missed clicks.
- `[answers]`, `[draft]`, `[claims]`: recordings folder, match threshold, draft mode, rule pack path.
- `[review]`, `[publish]`, `[static]`, `[shopify]`: the switches above and where pages go.
- `[livecheck]`: where to fetch pages for the check (the public URL, a preview server, or the written file) and retries.
- `[report]`: weekday, delivery and payload key.

## Repo layout

```
src/seo_engine/
  cli.py            commands: demo, init, check, approve, run, report
  engine.py         one run: search data, live gate, plan, action, report
  planner.py        the action ladder and the refusals
  opportunities.py  ranking by missed clicks
  answers.py        recorded answers and the approval gate
  page.py           page builder, HTML/Markdown rendering, source marker
  draft.py          template and model drafts with the grounding check
  claims.py         rule pack, claim check and safe fixes
  gates.py          the recording and claims gates
  review.py         GitHub pull requests, the GitHub gates and the merge
  livecheck.py      the live-page check
  report.py         weekly report and webhook
  searchconsole.py  Search Console client; search.py: rows, windows, CSV
  publish/          static and Shopify publishers
  state.py          state/ files and the ledger
  demo.py           synthetic data and the in-memory GitHub
  templates/        example config, rule pack, answer, and the demo recordings
tests/              pytest, fake transports only
examples/           scheduling (cron, systemd, launchd) and a sample CSV
```

## Development

```bash
uv run pytest
uv run ruff check
```

Runtime code uses only the Python standard library. The `llm` extra adds the Anthropic SDK for the model draft.

## License

MIT. See [LICENSE](LICENSE).
