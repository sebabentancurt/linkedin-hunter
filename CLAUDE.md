# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## What this project does

LinkedIn Hunter is an automated job search and Easy Apply pipeline. It:
1. Searches LinkedIn using configurable queries and scores results
2. Queues high-scoring Easy Apply jobs
3. Fills and submits Easy Apply forms automatically (CV upload, personal data, common questions)
4. Outputs a JSON queue and a CSV log of all jobs found

## Key commands

```bash
# Verify session is still valid
python3 check_session.py

# Save/refresh LinkedIn session (opens a visible browser)
python3 save_session.py            # persistent profile (default)
python3 save_session.py --cookies  # cookie file (required for GitHub Actions)

# Run the full pipeline
python3 daily.py                   # collect + apply
python3 daily.py collect           # search only
python3 daily.py apply             # apply from existing queue
python3 daily.py collect --full    # full scan (ignores seen jobs)

# Apply to a single job
python3 apply.py "<job_url>" "<cv_path>" --title "Role Name"
python3 apply.py "<job_url>" "<cv_path>" --dry-run
```

## Architecture

```
daily.py          — pipeline orchestrator (collect + apply)
apply.py          — Easy Apply automation (single job or called by daily.py)
save_session.py   — saves LinkedIn session interactively
check_session.py  — verifies session is still valid
config.toml       — all user configuration (queries, scoring, CV, personal data)
lib/
  auth.py         — session expiry detection
  browser.py      — Playwright context factory (persistent/cookies/public modes)
  search.py       — scraping, scoring, CV selection
```

## Output files (gitignored)

| File | Description |
|------|-------------|
| `linkedin_jobs_queue.json` | Current queue; statuses: `pending`, `applied`, `manual`, `error`, `low_score` |
| `linkedin_jobs_log.csv` | Append-only log of all jobs found |
| `linkedin_seen.json` | Job IDs already processed (skip list) |
| `logs/linkedin.log` | Detailed run log |

## Configuration

All config lives in `config.toml`. Key sections:
- `[stack_keywords]` — keyword → score weight (title matches count double)
- `[cv]` — single CV path or directory with `[[cv_rules]]` for rule-based selection
- `[personal_data]` — autofilled in Easy Apply forms
- `[form_answers]` — answers to common Easy Apply questions
- `[profile]` — used by LLM fallback (`claude -p`) for unknown form questions
- `[timing]` — request delays; increase if hitting captchas

## Session modes

- `"persistent"` — Chromium profile in `linkedin_user_data/` (default, best for local use)
- `"cookies"` — cookie file `linkedin_session.json` (required for GitHub Actions / headless CI)
- `"public"` — no session; public listings only (no Easy Apply)

## GitHub Actions

`.github/workflows/daily.yml` runs Mon–Fri at 11:00 UTC.
Requires a repository secret `LINKEDIN_SESSION_JSON` containing the content of `linkedin_session.json`.
Artifacts (queue + log) are uploaded after each run.

## LLM fallback

`apply.py` can call `claude -p` to answer form questions not covered by `[form_answers]`.
Requires Claude Code installed. Disable with `llm_fallback = false` in config.toml.

## Common tasks for Claude

- **Adding a new keyword**: edit `[stack_keywords]` in `config.toml`
- **Supporting a new form question**: add a tuple to `_KEYWORD_RULES` in `apply.py`
- **Debugging a failed apply**: check `logs/linkedin.log` and the screenshots saved to `screenshots_dir`
- **Refreshing session in CI**: re-run `save_session.py --cookies` locally, update the `LINKEDIN_SESSION_JSON` secret
