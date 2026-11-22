"""Build the daily summary the coach reads: recent days, baselines and flags."""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from statistics import mean
from typing import Any

TREND_METRICS = [
    "resting_hr",
    "hrv_last_night",
    "sleep_min",
    "sleep_score",
    "stress_avg",
    "body_battery_high",
    "training_readiness",
    "steps",
    "weight_kg",
]

CSV_COLUMNS = [
    "day",
    "resting_hr",
    "hrv_last_night",
    "hrv_status",
    "sleep_min",
    "sleep_deep_min",
    "sleep_rem_min",
    "sleep_score",
    "stress_avg",
    "body_battery_high",
    "body_battery_low",
    "training_readiness",
    "steps",
    "active_kcal",
    "intensity_min_moderate",
    "intensity_min_vigorous",
    "weight_kg",
    "body_fat_pct",
]


def _avg(days: dict[str, dict[str, Any]], metric: str, start: date, end: date) -> float | None:
    vals = [
        m[metric]
        for d, m in days.items()
        if start.isoformat() <= d <= end.isoformat() and isinstance(m.get(metric), (int, float))
    ]
    return round(mean(vals), 1) if vals else None


def flags(today: dict[str, Any], base: dict[str, dict[str, Any]]) -> list[str]:
    """Simple warning signs that argue for an easier day."""
    out = []
    hrv, low = today.get("hrv_last_night"), today.get("hrv_baseline_low")
    if isinstance(hrv, (int, float)) and isinstance(low, (int, float)) and hrv < low:
        out.append("HRV unter persönlichem Normalbereich")
    rhr, rhr28 = today.get("resting_hr"), base.get("resting_hr", {}).get("avg_28d")
    if isinstance(rhr, (int, float)) and isinstance(rhr28, (int, float)) and rhr >= rhr28 + 5:
        out.append("Ruhepuls mindestens 5 Schläge über 28-Tage-Schnitt")
    sleep = today.get("sleep_min")
    if isinstance(sleep, (int, float)) and sleep < 360:
        out.append("Weniger als 6 h Schlaf")
    bb = today.get("body_battery_high")
    if isinstance(bb, (int, float)) and bb < 50:
        out.append("Body Battery am Morgen unter 50")
    return out


def build(days: dict[str, dict[str, Any]], today: date, detail_days: int = 14) -> dict[str, Any]:
    latest_day = max((d for d in days if d <= today.isoformat()), default=None)
    latest = days.get(latest_day, {}) if latest_day else {}
    baselines = {
        m: {
            "latest": latest.get(m),
            "avg_7d": _avg(days, m, today - timedelta(days=6), today),
            "avg_28d": _avg(days, m, today - timedelta(days=27), today),
        }
        for m in TREND_METRICS
    }
    start = (today - timedelta(days=detail_days - 1)).isoformat()
    recent = {}
    for d in sorted(days):
        if d < start:
            continue
        m = dict(days[d])
        # Intraday HR only for the last 3 days, to keep the file small.
        if d < (today - timedelta(days=2)).isoformat():
            m.pop("hr_hourly", None)
        recent[d] = m
    return {
        "generated_for": today.isoformat(),
        "latest_day": latest_day,
        "flags": flags(latest, baselines),
        "baselines": baselines,
        "days": recent,
    }


def to_csv(days: dict[str, dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for d in sorted(days):
        writer.writerow({"day": d, **days[d]})
    return buf.getvalue()
