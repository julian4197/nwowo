"""healthsync: pull Garmin health data, store it, publish a daily summary.

Commands:
  login        one-time interactive login (asks for the MFA code), saves tokens
  sync         fetch the last DAYS days once and publish the summary
  run          stay running and sync at the times in SYNC_TIMES
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import garmin, store, summary

log = logging.getLogger("healthsync")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
TOKENSTORE = os.getenv("GARMINTOKENS", str(DATA_DIR / "garmin_tokens"))
DB_PATH = DATA_DIR / "health.db"
OUT_DIR = DATA_DIR / "out"


def cmd_login() -> int:
    if not os.getenv("GARMIN_EMAIL") or not os.getenv("GARMIN_PASSWORD"):
        print("GARMIN_EMAIL und GARMIN_PASSWORD müssen in .env stehen.", file=sys.stderr)
        return 1
    garmin.make_client(TOKENSTORE, prompt_mfa=lambda: input("Garmin MFA-Code: ").strip())
    print(f"Login ok, Tokens gespeichert in {TOKENSTORE}")
    return 0


def publish(files: list[Path]) -> None:
    remote = os.getenv("RCLONE_REMOTE")  # e.g. "gdrive:Workout-Coach"
    if not remote:
        log.info("RCLONE_REMOTE not set, skipping upload")
        return
    for f in files:
        subprocess.run(["rclone", "copyto", str(f), f"{remote}/{f.name}"], check=True)
    log.info("Uploaded %s to %s", ", ".join(f.name for f in files), remote)


def sync(days: int) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = store.connect(str(DB_PATH))
    client = garmin.make_client(TOKENSTORE)
    today = date.today()
    for i in range(days):
        day = today - timedelta(days=i)
        raw = garmin.fetch_day(client, day)
        store.save_raw(con, day.isoformat(), "garmin", raw)
        store.save_daily(con, day.isoformat(), "garmin", garmin.flatten(raw))
        log.info("Garmin %s synced", day)

    all_days = store.load_daily(con, (today - timedelta(days=90)).isoformat())
    summary_path = OUT_DIR / "health_summary.json"
    csv_path = OUT_DIR / "health_daily.csv"
    summary_path.write_text(
        json.dumps(summary.build(all_days, today), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    csv_path.write_text(summary.to_csv(all_days), encoding="utf-8")
    publish([summary_path, csv_path])


def next_run(now: datetime, times: list[str]) -> datetime:
    candidates = []
    for t in times:
        h, m = (int(x) for x in t.split(":"))
        c = now.replace(hour=h, minute=m, second=0, microsecond=0)
        candidates.append(c if c > now else c + timedelta(days=1))
    return min(candidates)


def cmd_run(days: int) -> int:
    times = [t.strip() for t in os.getenv("SYNC_TIMES", "07:30,13:00,21:30").split(",") if t.strip()]
    while True:
        try:
            sync(days)
        except Exception:
            log.exception("Sync failed, retrying at the next scheduled time")
        wake = next_run(datetime.now(), times)
        log.info("Next sync at %s", wake.strftime("%Y-%m-%d %H:%M"))
        time.sleep(max(60, (wake - datetime.now()).total_seconds()))


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="healthsync")
    p.add_argument("command", choices=["login", "sync", "run"])
    p.add_argument("--days", type=int, default=int(os.getenv("SYNC_DAYS", "3")),
                   help="how many recent days to (re)fetch per sync")
    a = p.parse_args()
    if a.command == "login":
        return cmd_login()
    if a.command == "sync":
        sync(a.days)
        return 0
    return cmd_run(a.days)


if __name__ == "__main__":
    sys.exit(main())
