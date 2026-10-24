# Setting up seo-engine (for a coding agent)

You are helping the owner of a small online business set up seo-engine on their site. Work through the steps in order, ask before each choice that is theirs to make, and run the check at the end of each step. Never write credentials into `seo-engine.toml`, into the repo, or into chat; they go in `.env` (git-ignored) or the environment of the scheduler. Never approve a recording on your own: approval means a person watched it and stands behind it.

## 1. Run the demo

```bash
uv run seo-engine demo
```

Walk the owner through the output: the plan ranked by missed clicks, the two refused queries (one with no recording, one with a recording nobody approved), the page built from the approved recording, the claim-check finding and its fix, the merge gates, the live check, and the weekly report in `seo-engine-demo/reports/`. Explain the central rule: the engine never writes a page without an approved recorded answer, so the way to get more pages is to record more answers. Done when the demo prints two `refused` lines and `Result: published`.

## 2. Create the config

Run `uv run seo-engine init`. It writes `seo-engine.toml`, `rules.toml` and an example answer in `answers/`. Ask:

1. **The site**: business name, the public origin (`base_url`, for example `https://www.example.com`) and where answer pages should live (`page_path`, default `/answers/{slug}/`).
2. **Brand terms**: the business name and its variants, so brand searches stay out of the opportunities.
3. **Where pages go** (`[publish] target`):
   - `static`: HTML (or Markdown) files in a folder that is served as it is or synced to a host. Page files and their parent directories inside this folder must not be symlinks. The filesystem must support directory-relative operations and no-follow opens.
   - `git`: the site is a git repo that the host deploys on every merge (Netlify, Cloudflare Pages, GitHub Pages and similar). Configure step 7's review prerequisites now, before selecting `target = "git"`; until then leave the target as `static`. Set `[static] format` and `file` to match the site generator, for example `format = "markdown"` and `file = "content/answers/{slug}.md"`, and make sure its templates render the front matter `title`, `description` and `canonical` in the page head. Keep deployment PRs open until step 8: any merge makes them live.
   - `shopify`: a Shopify page per answer. Set `page_path = "/pages/{slug}"`.

Leave `[publish] mode = "dry-run"`. Shopify previews need no store credentials. Done when `uv run seo-engine check` prints `Config OK` and the site you agreed; missing Search credentials are resolved in step 3. Malformed answer files make the check fail and must be corrected.

## 3. Connect Search Console

1. In Google Cloud, pick or create a project, enable the **Google Search Console API**, create a service account and download a JSON key. Save it outside the repo (for example `~/.config/seo-engine/search-console.json`) and run `chmod 600` on it.
2. In Search Console, open the property, then Settings, Users and permissions, Add user: the service account's email address with **Restricted** permission. That is read access, which is all the engine needs.
3. Put `GSC_SERVICE_ACCOUNT_FILE=<path to the key>` in `.env` and set `[search] site_url` to the property exactly as Search Console names it: `sc-domain:example.com` for a domain property, `https://www.example.com/` for a URL-prefix property.

The token request is signed with the `openssl` command, which macOS and most Linux systems have. If the owner cannot add a service account, use `[search] source = "csv"` with a daily export in the format of `examples/search-console.csv`. Each row needs explicit finite nonnegative clicks, impressions and position. Use numeric zeros for confirmed empty dates; blank cells do not mean zero and cause the source to be reported as unavailable.

The CLI reads `.env` from the folder that holds `seo-engine.toml`; a variable already set in the environment wins.

Done when `uv run seo-engine check` shows `env set` for Search, and `uv run seo-engine run` prints `Search data:` with a row count. A new site may have few rows; that is normal.

## 4. Write the rule pack with the owner

Go through `rules.toml` line by line with the owner:

- **Banned phrases**: promises (guarantees, "risk-free"), superlatives they cannot prove, and any words their industry or market restricts.
- **Claims that need a source**: percentages, research claims, legal or code requirements, anything else they want backed by a link.
- **Disclosures**: the text every page must carry. Use the owner's wording; do not write legal text for them.

Done when `uv run seo-engine check` lists the rule pack with the owner's rules.

## 5. Add recorded answers

A recorded answer is a customer question someone on the team answered on video. For each one, collect from the owner:

- the question as a customer would type it into a search box;
- the speaker's name and role, exactly as they should appear on the page;
- the public video URL (YouTube, Vimeo, or an https link to a video file) and optionally a thumbnail URL;
- the recording date;
- the transcript, from their video tool or a local transcription, unedited apart from fixing obvious transcription errors;
- every line the final video edit cut (`cut`), so those words never reach the page;
- optional `sources`: links the owner approves, each with the transcript wording it backs (`supports`);
- optional `target_queries`: search queries this answer is meant for.

Write `answers/<id>.json` and `answers/<id>.txt` (copy the shape of `src/seo_engine/templates/demo/answers/ra-001.json`, without copying its approval), and delete the example answer. Then ask the owner to watch each recording and approve it. Run `uv run seo-engine approve <id> --by <name>` only for a recording the owner has told you they watched and approve. Approval binds the transcript and publication metadata: question, speaker, video, thumbnail, recording date, cuts, sources and target queries. Changing them needs a new approval. Older approvals without a revision hash also need this explicit approval; a pending build from an older revision must be rebuilt and reviewed.

Done when `uv run seo-engine check` lists each recording as `approved`.

## 6. Dry runs

Run `uv run seo-engine run` (still in dry-run). Read the output with the owner:

- `refused` lines are questions with search demand and no approved recording. They are the owner's recording list.
- The built page is in `state/drafts/<slug>.html`. Open it with the owner and check the speaker, the direct answer, the video and the sources.
- A `held` page has claim-check findings that could not be fixed safely. Change the recording or the rule pack with the owner. Never edit the planner, the builder or the gates to get a page through.
- If a pending build becomes obsolete, approve the replacement revision and close the old PR. One run records that closure; the following run can build and review the new revision. Closing the old PR does not reject a newer recording revision.

Repeat for a few days. Done when the owner is happy with the drafts.

## 7. Review on GitHub (recommended; required for `target = "git"`)

1. Pick the repo: for `git`, the site repo; for `static` or `shopify`, any private repo that should keep the page source as a record of what was published.
2. The owner creates a fine-grained token for that one repo with Contents read and write, Pull requests read and write, and Checks read. Put it in `.env` as `GITHUB_TOKEN`.
3. Set `[review] enabled = true`, `repo = "owner/name"`, `base`, `path` (the folder inside the repo) and `required_checks` (the names of CI checks that must pass, for example the site build). Use unique check names: two providers with the same required name block merging. Boolean switches must be TOML booleans, such as `false`, without quotes.
4. Keep `auto_merge = false`. For static and Shopify, a person merges each pull request; the next live run publishes only if the merged files are exactly the build the engine approved. For Git deployments, keep the PR open until step 8: merging deploys immediately, including while the engine is in dry-run.

Done when a run opens a pull request and prints its merge gates. `auto-merge` (and `publish mode`, while in dry-run) fail by design; `checks` passes once CI has run on the branch.

## 8. Go live

Only when the owner explicitly asks:

1. For Shopify: the owner creates a custom app with the `read_content` and `write_content` Admin API scopes only, and puts `SHOPIFY_STORE` (the `your-store.myshopify.com` domain) and `SHOPIFY_ADMIN_TOKEN` in `.env`.
2. Set `[publish] mode = "live"`. For `git`, raise `[livecheck] attempts` and `wait_seconds` (for example 10 and 30) so the live check covers the deploy.
3. Optionally, after the owner has reviewed several pull requests, set `auto_merge = true` so the engine merges when every gate passes.

For Git, a person may now merge the reviewed deployment PR; the merge is the deployment boundary. If its live check fails, revert the PR manually. The engine reports the failed deployment and cannot automatically undo a merge.

Done when a run prints `Result: published` and `Live check ...: passed`. On Shopify, the first live publish also proves the theme keeps the page body as sent: if the live check fails on the schema or the source marker, the engine hides the page again and the run says which check failed. Existing Shopify pages stay hidden while body and SEO changes are applied. Static and Shopify rollback can make a previous page public only when its saved build matches the recovery snapshot and its current approval and claims gates pass; otherwise static recovery removes the verified file written by the run, and Shopify keeps its page hidden. All targets refuse collisions with content whose engine ownership cannot be verified. Successful static publications save ownership of the exact site folder and file paths so later body corruption can be repaired safely. The publisher captures the file binding before the live check and verifies it afterward; a changed binding leaves publication pending for operator recovery.

## 9. Schedule one run a day

Pick the example for their system in `examples/scheduling/` (cron, systemd or launchd), replace `/path/to/seo-engine` and `/path/to/uv` (`which uv`), and install it. `seo-engine run` exits 1 when a run has a blocker, so point the scheduler's failure alerts at it if they have any.

Done when `state/last-run.json` updates each day.

## 10. Optional: deliver the weekly report

Set `[report] send = "webhook"` and put a valid https incoming-webhook URL in `SEO_ENGINE_REPORT_WEBHOOK` (Slack uses the default `payload_key = "text"`; Discord uses `"content"`). Test with `uv run seo-engine report --send`. Collection and delivery share the run lock; a lock or initialization failure prevents sending and does not consume the week's delivery. The command exits 1 on blockers. The report goes out on `[report] weekday`, at most once for each reporting week. If a response is lost, delivery stays held for reconciliation, including for another `report --send`. Malformed URLs are rejected before recording delivery intent, and request errors never include the URL or raw exception message.

## 11. Optional: the model draft

`uv sync --extra llm`, put `ANTHROPIC_API_KEY` in `.env` and set `[draft] mode = "model"`. A model attempt is reused for the same recording revision and draft settings; repeated previews do not repeat a charge. The model selects whole sentences in transcript order, with only deterministic filler-sound cleanup. Any shortened, joined or rearranged sentence is rejected and the template is used.

## Recovering interrupted operations

Stop the scheduler and confirm no run is active before touching recovery state. Preserve corrupt files and restore them from a known good copy. A leftover `state/run.lock` or journal lock requires checking the interrupted operation before removing that lock; never clear pending journals just to force a retry.

- `state/github-writes.json` records GitHub writes. The next run can reconcile a lost branch or PR response by reading the matching repository, branch and head. If reconciliation is inconclusive, keep it held until the remote outcome is verified.
- Each page's `publication` entry in `state/pages.json` records its undo snapshot before Shopify mutations. Verify the remote page and restore its saved body and SEO fields, or keep a new page hidden. Restoring old visibility still requires the old build's current approval and claims gates; withdrawal, changed metadata, new claim findings or a missing saved build leave it hidden. Only mark that publication `done` after confirming recovery; an uncertain publication is never retried automatically.
- Missing or corrupt saved builds are preserved and reported per page while other live checks and the report continue. Restore the build from a known good copy before publishing it. Static `ownership` records bind a recording to its exact site folder and files; do not copy them to new destinations to bypass a collision. If a legacy file is already damaged and has no saved ownership record, verify its ownership before reconciling the record.
- Static publication recovery records include the destination, directory and file identities, and previous content. If a file or directory was replaced during publication, leave the operation pending until its outcome is established. Recovery never follows a substituted symlink or grants it ownership. If recovery removed a file because the previous build failed release gates, restore the approved recording, rules and saved build before publishing again.
- An unreadable or malformed `state/report.json` blocks scheduled reporting and is preserved for recovery. Completed publication state and `state/last-run.json` are still saved. Restore a valid checkpoint with the last confirmed reporting week before resuming scheduled reporting.
- `state/report-deliveries.json` records delivery per reporting week. Check the receiver before resolving a pending entry. If receipt is confirmed, mark it `done` with `result: null`; only remove an entry for an intentional retry when non-delivery is established. If the receiver cannot establish the outcome, leave it pending.
- `state/model-drafts.json` records model attempts. An interrupted request with unknown billing or response stays held; retain it until its outcome is resolved.
