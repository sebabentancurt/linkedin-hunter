"""
LinkedIn Hunter — daily pipeline.

Usage:
    python3 daily.py                 # collect only (default)
    python3 daily.py collect         # search and save queue only
    python3 daily.py collect --full  # full scan ignoring seen (uses posted_within_seconds window)
    python3 daily.py apply           # apply from existing queue (requires session + CV configured)
"""
import csv
import json
import logging
import os
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

_MODULE_DIR = Path(__file__).parent
sys.path.insert(0, str(_MODULE_DIR))

from lib.search import search_linkedin, load_seen, save_seen

CONFIG_FILE = _MODULE_DIR / "config.toml"
QUEUE_FILE  = _MODULE_DIR / "linkedin_jobs_queue.json"
LOG_FILE    = _MODULE_DIR / "linkedin_jobs_log.csv"

CMD       = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
FULL_SCAN = "--full" in sys.argv

_LOG_DIR = _MODULE_DIR / "logs"
_LOG_DIR.mkdir(exist_ok=True)


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("linkedin")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")

    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(_LOG_DIR / "linkedin.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = _setup_logger()


def _set_console_level(level_str: str):
    level = getattr(logging, level_str.upper(), logging.INFO)
    for h in log.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(level)
            break


# ── config ───────────────────────────────────────────────────────────────────

def _validate_config(cfg: dict):
    errors = []
    warnings = []

    session_mode = cfg.get("session_mode", "persistent")

    if session_mode == "cookies":
        session_file = _MODULE_DIR / "linkedin_session.json"
        if not session_file.exists():
            errors.append(f"session_mode='cookies' but linkedin_session.json not found. Run: python3 save_session.py --cookies")
    elif session_mode == "persistent":
        user_data_dir = _MODULE_DIR / "linkedin_user_data"
        if not user_data_dir.exists():
            errors.append(f"session_mode='persistent' but linkedin_user_data/ not found. Run: python3 save_session.py")

    cv_cfg = cfg.get("cv", {})
    cv_path = cv_cfg.get("path", "")
    cv_dir  = cv_cfg.get("dir", "")
    if not cv_path and not cv_dir:
        warnings.append("No CV configured. Set [cv] path or dir in config.toml — Easy Apply will skip CV upload.")
    elif cv_path:
        p = Path(cv_path) if Path(cv_path).is_absolute() else _MODULE_DIR / cv_path
        if not p.is_file():
            errors.append(f"CV file not found: {p}. Update [cv] path in config.toml.")

    personal = cfg.get("personal_data", {})
    missing_fields = [k for k in ("firstName", "lastName", "email") if not personal.get(k)]
    if missing_fields:
        warnings.append(f"personal_data missing: {', '.join(missing_fields)} — Easy Apply forms may be incomplete.")

    if not cfg.get("search_queries_local") and not cfg.get("search_queries_remote"):
        errors.append("No search queries defined. Add search_queries_local or search_queries_remote in config.toml.")

    for w in warnings:
        log.warning(f"Config: {w}")
    if errors:
        for e in errors:
            log.error(f"Config: {e}")
        sys.exit(1)


def load_config() -> dict:
    with open(CONFIG_FILE, "rb") as f:
        cfg = tomllib.load(f)
    if "log_level" in cfg:
        _set_console_level(cfg["log_level"])
    _validate_config(cfg)
    return cfg


# ── queue ─────────────────────────────────────────────────────────────────────

def _atomic_write_json(path, data, **kwargs):
    dir_ = os.path.dirname(path) or "."
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8") as f:
        json.dump(data, f, **kwargs)
        tmp = f.name
    os.replace(tmp, path)


def save_queue(jobs: list, threshold: int):
    payload = []
    for job in jobs:
        score   = job["score"]
        is_easy = job.get("isEasyApply", False)
        if score >= threshold and is_easy:
            status = "pending"
        elif score >= threshold:
            status = "manual"
        else:
            status = "low_score"
        payload.append({
            "jobId":      job["jobId"],
            "title":      job["title"],
            "company":    job.get("company", ""),
            "location":   job.get("location", ""),
            "link":       job.get("link", ""),
            "score":      score,
            "isEasyApply": is_easy,
            "cv_path":    job.get("cv_path", ""),
            "lang":       job.get("lang", "en"),
            "status":     status,
        })
    _atomic_write_json(str(QUEUE_FILE), payload, indent=2, ensure_ascii=False)
    pending_count = sum(1 for j in payload if j["status"] == "pending")
    log.info(f"Queue saved: {len(payload)} jobs ({pending_count} Easy Apply pending)")


def load_queue() -> list:
    if not QUEUE_FILE.exists():
        return []
    with open(QUEUE_FILE, encoding="utf-8") as f:
        return json.load(f)


def _flush_queue(queue: list):
    _atomic_write_json(str(QUEUE_FILE), queue, indent=2, ensure_ascii=False)


def _update_queue_status(job_id: str, status: str, queue: list):
    for job in queue:
        if job["jobId"] == job_id:
            job["status"] = status
            return


# ── CSV log ──────────────────────────────────────────────────────────────────

_CSV_FIELDS = ["date", "jobId", "title", "company", "location", "score", "isEasyApply", "status", "link"]


def _load_csv() -> dict:
    """Return existing CSV rows as {jobId: row_dict}."""
    if not LOG_FILE.exists():
        return {}
    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        return {row["jobId"]: row for row in csv.DictReader(f) if row.get("jobId")}


def _write_csv(rows: dict):
    dir_ = os.path.dirname(LOG_FILE) or "."
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp",
                                     newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows.values())
        tmp = f.name
    os.replace(tmp, str(LOG_FILE))


def _upsert_log(jobs: list, today: str, threshold: int):
    rows = _load_csv()
    for job in jobs:
        job_id  = job.get("jobId", "")
        score   = job.get("score", 0)
        is_easy = job.get("isEasyApply", False)
        if score >= threshold and is_easy:
            status = "Pending"
        elif score >= threshold:
            status = "Manual"
        else:
            status = "Low score"
        existing = rows.get(job_id)
        # Preserve first-seen date and any terminal status (Applied/Error)
        terminal = {"Applied", "Error"}
        if existing and existing.get("status") in terminal:
            continue
        rows[job_id] = {
            "date":        existing["date"] if existing else today,
            "jobId":       job_id,
            "title":       job.get("title", ""),
            "company":     job.get("company", ""),
            "location":    job.get("location", ""),
            "score":       score,
            "isEasyApply": is_easy,
            "status":      status,
            "link":        job.get("link", ""),
        }
    _write_csv(rows)


def _sync_csv_statuses(queue: list):
    """Update CSV statuses to reflect final apply results from the queue."""
    status_map = {"applied": "Applied", "error": "Error", "manual": "Manual"}
    rows = _load_csv()
    changed = False
    for job in queue:
        job_id = job.get("jobId", "")
        csv_status = status_map.get(job["status"])
        if csv_status and job_id in rows and rows[job_id]["status"] != csv_status:
            rows[job_id]["status"] = csv_status
            changed = True
    if changed:
        _write_csv(rows)


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_collect():
    config = load_config()
    today  = datetime.now().strftime("%Y-%m-%d")

    if FULL_SCAN:
        config["include_seen"]     = True
        config["max_local_queries"] = None
        config["max_jobs_per_run"] = None
        days = config.get("posted_within_seconds", 604800) // 86400
        log.info(f"=== collect {today} [FULL SCAN {days}d] ===")
    else:
        log.info(f"=== collect {today} ===")

    log.info("Searching jobs...")
    try:
        jobs = search_linkedin(config=config)
    except KeyboardInterrupt:
        log.warning("Interrupted by user during search.")
        return

    if not jobs:
        log.info("No new jobs found.")
        return

    seen = load_seen()
    seen.update(j["jobId"] for j in jobs)
    save_seen(seen)

    log.info(f"{len(jobs)} jobs found.")

    threshold = config.get("score_threshold", 5)
    to_apply  = [j for j in jobs if j["score"] >= threshold and j.get("isEasyApply")]
    manual    = [j for j in jobs if j["score"] >= threshold and not j.get("isEasyApply")]
    low_score = [j for j in jobs if j["score"] < threshold]

    log.info(f"  Easy Apply pending: {len(to_apply)}")
    log.info(f"  Manual:             {len(manual)}")
    log.info(f"  Low score:          {len(low_score)}")

    _upsert_log(jobs, today, threshold)
    save_queue(jobs, threshold)


def cmd_apply():
    from apply import apply_easy_apply

    queue   = load_queue()
    pending = [j for j in queue if j["status"] == "pending"]

    if not pending:
        log.info("No pending jobs in queue.")
        if not queue:
            log.info("(empty queue — run first: daily.py collect)")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    log.info(f"=== apply {today} ===")
    log.info(f"{len(pending)} jobs pending Easy Apply")

    applied = manual_fallback = errors = 0

    try:
        for job in pending:
            log.info(f"Applying: {job['title']} - {job['company']}")
            result = apply_easy_apply(
                job_url=job["link"],
                cv_path=job["cv_path"],
                job_title=job["title"],
            )
            if result == "applied":
                _update_queue_status(job["jobId"], "applied", queue)
                log.info(f"  → Applied: {job['title']}")
                applied += 1
            elif result == "manual":
                _update_queue_status(job["jobId"], "manual", queue)
                log.info(f"  → Manual: {job['title']}")
                manual_fallback += 1
            else:
                _update_queue_status(job["jobId"], "error", queue)
                log.error(f"  → Error: {job['title']}")
                errors += 1
            _flush_queue(queue)
    except KeyboardInterrupt:
        _flush_queue(queue)
        log.warning("Interrupted by user — queue saved.")
        return

    manual_total = sum(1 for j in queue if j["status"] == "manual")
    log.info(f"=== SUMMARY: applied={applied} manual={manual_total} errors={errors} ===")
    _sync_csv_statuses(queue)


def run():
    cmd_collect()
    print()
    cmd_apply()


if __name__ == "__main__":
    if CMD == "apply":
        cmd_apply()
    else:
        cmd_collect()  # default: collect only (pass "apply" or edit daily.py to enable auto-apply)
