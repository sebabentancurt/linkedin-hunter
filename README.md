# LinkedIn Hunter

Automated LinkedIn job search and Easy Apply pipeline.

Searches LinkedIn using your configured queries, scores each job against your skill stack, and automatically fills and submits Easy Apply forms — including CV upload, personal data, and common screening questions. Runs daily via GitHub Actions or a local cron job.

---

## How it works

1. **Collect** — searches LinkedIn using your queries, fetches job descriptions, scores each result
2. **Queue** — high-scoring Easy Apply jobs go into `linkedin_jobs_queue.json`
3. **Apply** — fills out Easy Apply forms step by step: personal data → CV → screening questions → submit
4. **Log** — appends all found jobs to `linkedin_jobs_log.csv`

Jobs that require manual steps (multi-page forms, custom questions) are flagged as `manual` in the queue.

---

## Requirements

- Python 3.11+
- A LinkedIn account (free tier is fine)

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure

Edit `config.toml`:

```toml
# Search queries
search_queries_local  = ["senior backend engineer", "tech lead"]
search_queries_remote = ["senior backend engineer remote", "tech lead latam"]

# Your local market geo ID (find it in the LinkedIn URL when searching by city)
geo_id = "102299470"

# Your CV
[cv]
path = "resume.pdf"

# Personal data for Easy Apply forms
[personal_data]
firstName = "Jane"
lastName  = "Doe"
email     = "jane@example.com"
phone     = "+1 555 0100"
city      = "Buenos Aires"
country   = "Argentina"

# Common screening question answers
[form_answers]
years_of_experience = "7"
work_authorization  = "Yes"
sponsorship_required = "No"
english_proficiency = "Professional"

# Score keywords — tune these to your stack (title matches count double)
[stack_keywords]
python   = 2
fastapi  = 3
"tech lead" = 2
aws      = 2
```

See `config.toml` for the full reference with all available options.

### 3. Save your LinkedIn session

```bash
python3 save_session.py
```

This opens a visible Chromium window. Log in to LinkedIn normally, then press Enter in the terminal. The session is saved to `linkedin_user_data/` and reused on future runs.

To verify the session is still valid:

```bash
python3 check_session.py
```

---

## Usage

```bash
# Full pipeline: search → score → Easy Apply
python3 daily.py

# Search only (no applications sent)
python3 daily.py collect

# Apply from the existing queue (no new search)
python3 daily.py apply

# Full scan — ignore seen jobs, use the full time window from config
python3 daily.py collect --full

# Apply to a single job manually
python3 apply.py "https://www.linkedin.com/jobs/view/1234567890/" resume.pdf
python3 apply.py "https://www.linkedin.com/jobs/view/1234567890/" resume.pdf --dry-run
```

---

## Output

| File | Description |
|------|-------------|
| `linkedin_jobs_queue.json` | Current queue — open this to review pending/applied/manual jobs |
| `linkedin_jobs_log.csv` | Append-only log of every job found (open in Excel or Google Sheets) |
| `linkedin_seen.json` | Job IDs already processed — prevents duplicates across runs |
| `logs/linkedin.log` | Detailed run log |

Queue statuses: `pending` (queued for Easy Apply) · `applied` · `manual` (needs human) · `error` · `low_score`

---

## GitHub Actions (scheduled runs)

The included workflow runs the pipeline automatically Mon–Fri at 11:00 UTC.

### Setup

1. Generate a cookie session file locally:

```bash
python3 save_session.py --cookies
# Saves linkedin_session.json
```

2. Add the file contents as a repository secret:
   - Go to **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `LINKEDIN_SESSION_JSON`
   - Value: paste the contents of `linkedin_session.json`

3. Push to GitHub — the workflow will run automatically on schedule.

The workflow uploads `linkedin_jobs_queue.json`, `linkedin_jobs_log.csv`, and logs as artifacts after each run (retained for 30 days).

> **Note:** Cookie sessions expire periodically. Re-run `save_session.py --cookies` and update the secret when jobs stop being found or you see session errors in the logs.

To trigger a run manually: **Actions → LinkedIn Hunter — Daily Run → Run workflow**.

---

## CV selection

**Single CV (simplest):**

```toml
[cv]
path = "resume.pdf"
```

**Multiple CVs with rule-based selection:**

```toml
[cv]
dir     = "resumes/"
default = "resume_backend.pdf"

[[cv_rules]]
file     = "resume_ai.pdf"
keywords = ["llm", "rag", "openai", "machine learning"]

[[cv_rules]]
file     = "resume_backend.pdf"
keywords = ["python", "fastapi", "django", "backend"]
```

The first matching rule wins. Falls back to `default` if no rule matches.

---

## LLM fallback

For form questions not covered by `[form_answers]`, the tool can call `claude -p` to answer them automatically. This requires [Claude Code](https://claude.ai/code) to be installed and authenticated.

Configure your profile so answers are contextual:

```toml
[profile]
name                = "Jane Doe"
years_of_experience = "7"
stack               = "Python, FastAPI, React, AWS, Docker, PostgreSQL"
location            = "Buenos Aires, Argentina"
languages           = "Spanish (native), English (fluent)"
```

Disable the fallback with `llm_fallback = false`.

---

## Scoring

Every job gets a score before being queued. Scoring factors:

- **Stack keywords** — defined in `[stack_keywords]`; title matches count double
- **Local bonus** — jobs in your area get `local_bonus` points
- **Radar companies** — companies in `radar_companies` get a bonus
- **Avoid companies** — companies in `avoid_companies` are dropped entirely
- **Avoid keywords** — jobs with these keywords in title/description get a penalty

Only jobs at or above `score_threshold` are queued. Easy Apply jobs go to `pending`; others go to `manual`.

---

## Notes

- This tool is for personal use. Automated scraping may violate LinkedIn's [Terms of Service](https://www.linkedin.com/legal/user-agreement). Use responsibly.
- The timing defaults are conservative. Increase `[timing]` delays if you encounter captchas or rate limits.
- Sessions expire periodically. Run `python3 check_session.py` to verify, and `python3 save_session.py` to refresh.
