"""healthsync: pull Garmin health data, store it, publish a daily summary.

Commands:
  login        one-time interactive login (asks for the MFA code), saves tokens
  sync         fetch the last DAYS days once and publish the summary
  backfill     fetch every day since SINCE (oldest first, skips days already stored)
  import-apple import an Apple Health export (export.zip) from /data/inbox
  push-plan    push plan.json (from Google Drive or --file) to Garmin Connect
  calendar     export upcoming iCloud calendar events to calendar_busy.json
  run          stay running and sync at the times in SYNC_TIMES
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import apple, calendar_busy, garmin, hae, healthfit, store, summary, workouts

log = logging.getLogger("healthsync")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
TOKENSTORE = os.getenv("GARMINTOKENS", str(DATA_DIR / "garmin_tokens"))
DB_PATH = DATA_DIR / "health.db"
OUT_DIR = DATA_DIR / "out"
FIT_DIR = DATA_DIR / "activities"
INBOX = DATA_DIR / "inbox"


def cmd_login() -> int:
    if not os.getenv("GARMIN_EMAIL") or not os.getenv("GARMIN_PASSWORD"):
        print("GARMIN_EMAIL und GARMIN_PASSWORD müssen in .env stehen.", file=sys.stderr)
        return 1
    garmin.make_client(TOKENSTORE, prompt_mfa=lambda: input("Garmin MFA-Code: ").strip())
    print(f"Login ok, Tokens gespeichert in {TOKENSTORE}")
    return 0


def _join(remote: str, name: str) -> str:
    return remote + name if remote.endswith(":") else f"{remote.rstrip('/')}/{name}"


def publish(files: list[Path]) -> None:
    remote = os.getenv("RCLONE_REMOTE")  # e.g. "gdrive:Workout-Coach"
    if not remote:
        log.info("RCLONE_REMOTE not set, skipping upload")
        return
    for f in files:
        subprocess.run(["rclone", "copyto", str(f), _join(remote, f.name)], check=True)
    log.info("Uploaded %s to %s", ", ".join(f.name for f in files), remote)


_lock = threading.Lock()


def _sync_day(client, con, day: date) -> None:
    raw = garmin.fetch_day(client, day)
    with _lock:
        store.save_raw(con, day.isoformat(), "garmin", raw)
        store.save_daily(con, day.isoformat(), "garmin", garmin.flatten(raw))
    log.info("Garmin %s synced", day)


def _sync_activities(client, con, start: date, end: date) -> None:
    acts = garmin.fetch_activities(client, start, end)
    with _lock:
        store.save_activities(con, acts)
    FIT_DIR.mkdir(parents=True, exist_ok=True)
    for a in acts:
        f = FIT_DIR / f"{a['activityId']}.zip"
        if not f.exists() and (data := garmin.download_fit(client, a["activityId"])):
            f.write_bytes(data)
    log.info("Garmin activities %s..%s: %d", start, end, len(acts))


def sync(days: int) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = store.connect(str(DB_PATH))
    client = garmin.make_client(TOKENSTORE)
    today = date.today()
    for i in range(days):
        _sync_day(client, con, today - timedelta(days=i))
    _sync_activities(client, con, today - timedelta(days=days - 1), today)
    con.close()
    try:
        with _lock:
            healthfit.sync(str(DB_PATH), DATA_DIR)
    except Exception:
        log.exception("HealthFit sheet import failed")
    publish_summary()


def cmd_import_apple(path: str | None) -> int:
    files = [Path(path)] if path else apple.find_export(INBOX)
    if not files:
        print(f"Keine export.zip in {INBOX} gefunden.", file=sys.stderr)
        return 1
    for f in files:
        apple.import_export(str(DB_PATH), str(f))
    publish_summary()
    return 0


def backfill(since: date, pause: float) -> None:
    """Fetch all history once. Safe to stop and restart: stored days are skipped."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = store.connect(str(DB_PATH))
    client = garmin.make_client(TOKENSTORE)
    end = date.today()
    start = since
    while start <= end:  # activities in 90-day chunks
        _sync_activities(client, con, start, min(start + timedelta(days=89), end))
        start += timedelta(days=90)
    day = since
    while day <= end:
        if not store.has_raw_day(con, day.isoformat(), "garmin"):
            _sync_day(client, con, day)
            time.sleep(pause)  # be gentle, Garmin rate-limits
        day += timedelta(days=1)
    con.close()
    publish_summary()


def publish_summary() -> None:
    with _lock:
        _publish_summary()


def _publish_summary() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = store.connect(str(DB_PATH))
    today = date.today()
    all_days = store.load_daily(con, (today - timedelta(days=90)).isoformat())
    summary_path = OUT_DIR / "health_summary.json"
    csv_path = OUT_DIR / "health_daily.csv"
    summary_path.write_text(
        json.dumps(summary.build(all_days, today), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    csv_path.write_text(summary.to_csv(all_days), encoding="utf-8")
    con.close()
    publish([summary_path, csv_path])


def publish_calendar() -> None:
    if not calendar_busy.enabled():
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "calendar_busy.json"
    path.write_text(json.dumps(calendar_busy.fetch(), ensure_ascii=False, indent=1), encoding="utf-8")
    publish([path])


def next_run(now: datetime, times: list[str]) -> datetime:
    candidates = []
    for t in times:
        h, m = (int(x) for x in t.split(":"))
        c = now.replace(hour=h, minute=m, second=0, microsecond=0)
        candidates.append(c if c > now else c + timedelta(days=1))
    return min(candidates)


PLAN_PATH = DATA_DIR / "plan.json"


def fetch_plan() -> str | None:
    """Return the newest plan*.json from RCLONE_REMOTE (by modification time).

    The coach uploads a new file for every plan change (it cannot edit files in
    place), so the newest one wins. rclone needs the "drive" scope for this,
    ideally limited to the Workout-Coach folder via root_folder_id.
    """
    remote = os.getenv("RCLONE_REMOTE")
    if not remote:
        return PLAN_PATH.read_text(encoding="utf-8") if PLAN_PATH.exists() else None
    r = subprocess.run(["rclone", "lsjson", "--files-only", remote], capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("Cannot list %s: %s", remote, r.stderr.strip())
        return None
    plans = [f for f in json.loads(r.stdout or "[]")
             if f.get("Name", "").startswith("plan") and f["Name"].endswith(".json")]
    if not plans:
        return None
    newest = max(plans, key=lambda f: f.get("ModTime", ""))
    text = subprocess.run(["rclone", "cat", _join(remote, newest["Name"])],
                          capture_output=True, text=True, check=True).stdout
    PLAN_PATH.write_text(text, encoding="utf-8")
    return text


def push_plan(text: str) -> dict[str, int]:
    plan = json.loads(text)
    con = store.connect(str(DB_PATH))
    client = garmin.make_client(TOKENSTORE)
    stats = workouts.push(con, client, plan)
    con.close()
    log.info("Plan pushed to Garmin: %s", stats)
    return stats


def cmd_push_plan(path: str | None) -> int:
    text = Path(path).read_text(encoding="utf-8") if path else fetch_plan()
    if text is None:
        print("Kein Plan gefunden.", file=sys.stderr)
        return 1
    print(json.dumps(push_plan(text)))
    return 0


def plan_watcher(interval_min: float) -> None:
    last = None
    while True:
        try:
            text = fetch_plan()
            if text is not None and text != last:
                push_plan(text)
                last = text
        except Exception:
            log.exception("Plan push failed, retrying later")
        time.sleep(interval_min * 60)


def cmd_run(days: int) -> int:
    plan_every = float(os.getenv("PLAN_CHECK_MINUTES", "15"))
    if plan_every > 0:
        threading.Thread(target=plan_watcher, args=(plan_every,), daemon=True).start()
    if hae.enabled():
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        def on_update():
            try:
                publish_summary()
            except Exception:
                log.exception("Publishing after Health Auto Export push failed")

        hae.serve(str(DB_PATH), int(os.environ["HAE_PORT"]), os.getenv("HAE_TOKEN") or None, on_update,
                  plan_path=str(PLAN_PATH))
    times = [t.strip() for t in os.getenv("SYNC_TIMES", "07:30,13:00,21:30").split(",") if t.strip()]
    while True:
        try:
            sync(days)
        except Exception:
            log.exception("Sync failed, retrying at the next scheduled time")
        try:
            publish_calendar()
        except Exception:
            log.exception("Calendar export failed")
        wake = next_run(datetime.now(), times)
        log.info("Next sync at %s", wake.strftime("%Y-%m-%d %H:%M"))
        time.sleep(max(60, (wake - datetime.now()).total_seconds()))


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="healthsync")
    p.add_argument("command", choices=["login", "sync", "backfill", "import-apple", "push-plan", "calendar", "run"])
    p.add_argument("--days", type=int, default=int(os.getenv("SYNC_DAYS", "3")),
                   help="how many recent days to (re)fetch per sync")
    p.add_argument("--since", type=date.fromisoformat, help="backfill start, e.g. 2019-01-01")
    p.add_argument("--pause", type=float, default=2.0, help="backfill seconds between days")
    p.add_argument("--file", default=None, help="import-apple: path to export.zip; push-plan: path to plan.json")
    a = p.parse_args()
    if a.command == "login":
        return cmd_login()
    if a.command == "sync":
        sync(a.days)
        return 0
    if a.command == "calendar":
        if not calendar_busy.enabled():
            print("ICLOUD_USER und ICLOUD_APP_PASSWORD fehlen in .env.", file=sys.stderr)
            return 1
        publish_calendar()
        return 0
    if a.command == "push-plan":
        return cmd_push_plan(a.file)
    if a.command == "import-apple":
        return cmd_import_apple(a.file)
    if a.command == "backfill":
        if not a.since:
            p.error("backfill needs --since YYYY-MM-DD")
        backfill(a.since, a.pause)
        return 0
    return cmd_run(a.days)


if __name__ == "__main__":
    sys.exit(main())
