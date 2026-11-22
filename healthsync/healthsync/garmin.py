"""Fetch daily health data from Garmin Connect and reduce it to flat metrics."""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Callable

log = logging.getLogger(__name__)


def make_client(tokenstore: str, prompt_mfa: Callable[[], str] | None = None):
    """Return a logged-in Garmin client.

    Uses saved tokens from ``tokenstore``. Email and password from the
    environment are only needed for the first login (or when tokens expire).
    """
    from garminconnect import Garmin  # imported lazily so tests run without it

    client = Garmin(
        email=os.getenv("GARMIN_EMAIL") or None,
        password=os.getenv("GARMIN_PASSWORD") or None,
        prompt_mfa=prompt_mfa,
    )
    client.login(tokenstore)
    return client


def _safe(fn: Callable[[], Any], what: str) -> Any:
    try:
        return fn()
    except Exception as e:  # one missing data type must not stop the sync
        log.warning("Garmin %s failed: %s", what, e)
        return None


def fetch_day(client, day: date) -> dict[str, Any]:
    """Return the raw Garmin responses for one day, keyed by kind."""
    d = day.isoformat()
    return {
        "summary": _safe(lambda: client.get_user_summary(d), "user summary"),
        "sleep": _safe(lambda: client.get_sleep_data(d), "sleep"),
        "hrv": _safe(lambda: client.get_hrv_data(d), "hrv"),
        "readiness": _safe(lambda: client.get_training_readiness(d), "training readiness"),
        "heart_rates": _safe(lambda: client.get_heart_rates(d), "heart rates"),
        "body": _safe(lambda: client.get_body_composition(d), "body composition"),
        "stress": _safe(lambda: client.get_stress_data(d), "stress"),
        "body_battery": _safe(lambda: client.get_body_battery_events(d), "body battery events"),
        "respiration": _safe(lambda: client.get_respiration_data(d), "respiration"),
        "spo2": _safe(lambda: client.get_spo2_data(d), "spo2"),
        "steps": _safe(lambda: client.get_steps_data(d), "steps"),
        "intensity": _safe(lambda: client.get_intensity_minutes_data(d), "intensity minutes"),
        "training_status": _safe(lambda: client.get_training_status(d), "training status"),
        "max_metrics": _safe(lambda: client.get_max_metrics(d), "max metrics"),
        "hydration": _safe(lambda: client.get_hydration_data(d), "hydration"),
    }


def fetch_activities(client, start: date, end: date) -> list[dict[str, Any]]:
    """Activity summaries started between ``start`` and ``end`` (inclusive)."""
    return _safe(lambda: client.get_activities_by_date(start.isoformat(), end.isoformat()), "activities") or []


def download_fit(client, activity_id: Any) -> bytes | None:
    """Original upload from the watch (a zip with the .fit file)."""
    fmt = client.ActivityDownloadFormat.ORIGINAL
    return _safe(lambda: client.download_activity(str(activity_id), dl_fmt=fmt), f"activity {activity_id} file")


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _minutes(seconds: Any) -> float | None:
    return round(seconds / 60, 1) if isinstance(seconds, (int, float)) else None


def hourly_hr(heart_rates: Any) -> dict[str, int]:
    """Average heart rate per local hour from Garmin's intraday values."""
    values = _get(heart_rates, "heartRateValues") or []
    start_gmt = _get(heart_rates, "startTimestampGMT")
    start_local = _get(heart_rates, "startTimestampLocal")
    offset_ms = 0
    if start_gmt and start_local:
        from datetime import datetime

        fmt = "%Y-%m-%dT%H:%M:%S.%f"
        try:
            offset_ms = int(
                (datetime.strptime(start_local, fmt) - datetime.strptime(start_gmt, fmt)).total_seconds()
                * 1000
            )
        except ValueError:
            offset_ms = 0
    buckets: dict[int, list[int]] = {}
    for entry in values:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2 or entry[1] is None:
            continue
        hour = int(((entry[0] + offset_ms) // 3_600_000) % 24)
        buckets.setdefault(hour, []).append(entry[1])
    return {f"{h:02d}": round(sum(v) / len(v)) for h, v in sorted(buckets.items())}


def flatten(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce the raw responses of one day to the metrics the coach uses."""
    s = raw.get("summary") or {}
    sleep = _get(raw.get("sleep"), "dailySleepDTO") or {}
    hrv = _get(raw.get("hrv"), "hrvSummary") or {}
    readiness = raw.get("readiness")
    if isinstance(readiness, list):
        readiness = readiness[0] if readiness else {}
    readiness = readiness or {}
    weights = _get(raw.get("body"), "dateWeightList") or []
    w = weights[-1] if weights else {}
    weight_g = w.get("weight") if isinstance(w, dict) else None

    return {
        "resting_hr": s.get("restingHeartRate"),
        "min_hr": s.get("minHeartRate"),
        "max_hr": s.get("maxHeartRate"),
        "steps": s.get("totalSteps"),
        "active_kcal": s.get("activeKilocalories"),
        "total_kcal": s.get("totalKilocalories"),
        "stress_avg": s.get("averageStressLevel"),
        "body_battery_high": s.get("bodyBatteryHighestValue"),
        "body_battery_low": s.get("bodyBatteryLowestValue"),
        "body_battery_charged": s.get("bodyBatteryChargedValue"),
        "body_battery_drained": s.get("bodyBatteryDrainedValue"),
        "intensity_min_moderate": s.get("moderateIntensityMinutes"),
        "intensity_min_vigorous": s.get("vigorousIntensityMinutes"),
        "sleep_min": _minutes(sleep.get("sleepTimeSeconds")),
        "sleep_deep_min": _minutes(sleep.get("deepSleepSeconds")),
        "sleep_rem_min": _minutes(sleep.get("remSleepSeconds")),
        "sleep_light_min": _minutes(sleep.get("lightSleepSeconds")),
        "sleep_awake_min": _minutes(sleep.get("awakeSleepSeconds")),
        "sleep_score": _get(sleep, "sleepScores", "overall", "value"),
        "hrv_last_night": hrv.get("lastNightAvg"),
        "hrv_weekly_avg": hrv.get("weeklyAvg"),
        "hrv_status": hrv.get("status"),
        "hrv_baseline_low": _get(hrv, "baseline", "balancedLow"),
        "hrv_baseline_high": _get(hrv, "baseline", "balancedUpper"),
        "training_readiness": readiness.get("score"),
        "training_readiness_level": readiness.get("level"),
        "weight_kg": round(weight_g / 1000, 2) if isinstance(weight_g, (int, float)) else None,
        "body_fat_pct": w.get("bodyFat") if isinstance(w, dict) else None,
        "hr_hourly": hourly_hr(raw.get("heart_rates")),
    }
