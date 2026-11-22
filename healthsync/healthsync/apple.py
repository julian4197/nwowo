"""Import the full Apple Health export (export.zip from the Health app).

Every quantity record is kept in ``apple_records``, sleep stages in
``apple_sleep`` and workouts in ``apple_workouts``. Weight, body composition,
nutrition, resting HR, HRV (SDNN), steps, VO2max and sleep duration are also
rolled up per day into ``daily`` (source "apple"), so Apple Watch, Zepp Life
and Yazio history shows up next to the Garmin data (Garmin wins on overlap).
"""
from __future__ import annotations

import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Iterator
from xml.etree.ElementTree import iterparse

from . import store

log = logging.getLogger(__name__)

PREFIX = "HKQuantityTypeIdentifier"

# Apple type (without prefix) -> (our field, combine, factor)
DAILY = {
    "BodyMass": ("weight_kg", "last", 1),
    "BodyFatPercentage": ("body_fat_pct", "last", 100),
    "LeanBodyMass": ("lean_mass_kg", "last", 1),
    "BodyMassIndex": ("bmi", "last", 1),
    "DietaryEnergyConsumed": ("kcal_in", "sum", 1),
    "DietaryProtein": ("protein_g", "sum", 1),
    "DietaryCarbohydrates": ("carbs_g", "sum", 1),
    "DietaryFatTotal": ("fat_g", "sum", 1),
    "DietaryFiber": ("fiber_g", "sum", 1),
    "DietaryWater": ("water_ml", "sum", 1),
    "RestingHeartRate": ("resting_hr", "last", 1),
    "StepCount": ("steps", "sum", 1),
    "VO2Max": ("vo2max", "last", 1),
}
# averaged per day
AVG = {"HeartRateVariabilitySDNN": "hrv_sdnn", "HeartRate": "hr_avg"}

SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
ASLEEP = ("Asleep", "AsleepCore", "AsleepDeep", "AsleepREM", "AsleepUnspecified")

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS apple_sleep (
    source TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, stage TEXT NOT NULL,
    PRIMARY KEY (source, start, stage)
);
CREATE TABLE IF NOT EXISTS apple_workouts (
    source TEXT NOT NULL, start TEXT NOT NULL, end TEXT, type TEXT,
    duration_min REAL, distance_km REAL, kcal REAL, payload TEXT,
    PRIMARY KEY (source, start, type)
);
"""


def _ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z").timestamp()


def _open(path: str) -> IO[bytes]:
    if path.endswith(".zip"):
        z = zipfile.ZipFile(path)
        name = next(n for n in z.namelist() if n.lower().endswith("export.xml") and "cda" not in n.lower())
        return z.open(name)
    return open(path, "rb")


def elements(path: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ("record", attrs), ("sleep", attrs) and ("workout", attrs+stats)."""
    with _open(path) as f:
        depth = 0
        for event, el in iterparse(f, events=("start", "end")):
            if event == "start":
                depth += 1
                continue
            depth -= 1
            if el.tag == "Record" and depth == 1:
                t = el.get("type", "")
                if t.startswith(PREFIX):
                    yield "record", dict(el.attrib)
                elif t == SLEEP:
                    yield "sleep", dict(el.attrib)
                el.clear()
            elif el.tag == "Workout":
                w: dict[str, Any] = dict(el.attrib)
                for st in el.findall("WorkoutStatistics"):
                    w["stat:" + st.get("type", "").replace(PREFIX, "")] = dict(st.attrib)
                yield "workout", w
                el.clear()


def records(path: str) -> Iterator[dict[str, str]]:
    for kind, attrs in elements(path):
        if kind == "record":
            yield attrs


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _workout_row(w: dict[str, Any]) -> tuple:
    dist = _num(w.get("totalDistance"))
    unit = w.get("totalDistanceUnit", "km")
    if dist is None:
        for key in ("stat:DistanceWalkingRunning", "stat:DistanceCycling", "stat:DistanceSwimming"):
            if key in w:
                dist, unit = _num(w[key].get("sum")), w[key].get("unit", "km")
                break
    if dist is not None and unit == "m":
        dist /= 1000
    if dist is not None and unit == "mi":
        dist *= 1.609344
    kcal = _num(w.get("totalEnergyBurned"))
    if kcal is None and "stat:ActiveEnergyBurned" in w:
        kcal = _num(w["stat:ActiveEnergyBurned"].get("sum"))
    dur = _num(w.get("duration"))
    if dur is not None and w.get("durationUnit") == "s":
        dur /= 60
    return (
        w.get("sourceName", ""), w.get("startDate", ""), w.get("endDate"),
        (w.get("workoutActivityType") or "").replace("HKWorkoutActivityType", ""),
        dur, round(dist, 3) if dist is not None else None, kcal, json.dumps(w),
    )


def import_export(db_path: str, path: str) -> int:
    con = store.connect(db_path)
    con.executescript(EXTRA_SCHEMA)
    per_day: dict[str, dict[str, float]] = {}
    avg: dict[tuple[str, str], list[float]] = {}
    sleep_min: dict[str, float] = {}
    n = 0
    batch = []
    sleeps, workouts = [], []
    for kind, r in elements(path):
        if kind == "sleep":
            stage = r.get("value", "").replace("HKCategoryValueSleepAnalysis", "")
            sleeps.append((r.get("sourceName", ""), r.get("startDate", ""), r.get("endDate", ""), stage))
            continue
        if kind == "workout":
            workouts.append(_workout_row(r))
            continue
        try:
            value = float(r.get("value", ""))
        except ValueError:
            continue
        kind = r["type"][len(PREFIX):]
        start = r.get("startDate", "")
        batch.append((kind, r.get("sourceName", ""), r.get("unit"), start, r.get("endDate"), value))
        if kind in AVG and len(start) >= 10:
            avg.setdefault((start[:10], AVG[kind]), []).append(value)
        spec = DAILY.get(kind)
        if spec and len(start) >= 10:
            field, how, factor = spec
            unit = (r.get("unit") or "").lower()
            if field == "weight_kg" and unit == "lb":
                value *= 0.45359237
            if field == "kcal_in" and unit == "kj":
                value /= 4.184
            if field == "water_ml" and unit == "l":
                value *= 1000
            row = per_day.setdefault(start[:10], {})
            v = value * factor
            row[field] = v if how == "last" or field not in row else row[field] + v
        if len(batch) >= 5000:
            con.executemany("INSERT OR IGNORE INTO apple_records VALUES (?, ?, ?, ?, ?, ?)", batch)
            con.commit()
            n += len(batch)
            batch = []
    con.executemany("INSERT OR IGNORE INTO apple_records VALUES (?, ?, ?, ?, ?, ?)", batch)
    con.executemany("INSERT OR IGNORE INTO apple_sleep VALUES (?, ?, ?, ?)", sleeps)
    con.executemany("INSERT OR IGNORE INTO apple_workouts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", workouts)
    con.commit()
    n += len(batch)
    for (day, field), vals in avg.items():
        per_day.setdefault(day, {})[field] = sum(vals) / len(vals)
    # Sleep per night, counted on the day it ends; only the source with most sleep that night.
    by_source: dict[tuple[str, str], float] = {}
    for src, st, en, stage in sleeps:
        if stage in ASLEEP and len(en) >= 10:
            mins = (_ts(en) - _ts(st)) / 60
            by_source[(en[:10], src)] = by_source.get((en[:10], src), 0) + mins
    for (day, _src), mins in by_source.items():
        sleep_min[day] = max(sleep_min.get(day, 0), mins)
    for day, mins in sleep_min.items():
        per_day.setdefault(day, {})["sleep_min"] = mins
    # Note: records arrive in export order (by type, then time), so "last" is the latest of the day.
    for day, fields in per_day.items():
        store.save_daily(con, day, "apple", {k: round(v, 2) for k, v in fields.items()})
    con.close()
    log.info("Apple Health: %d records, %d sleep entries, %d workouts, %d days", n, len(sleeps), len(workouts), len(per_day))
    return n


def find_export(inbox: Path) -> list[Path]:
    return sorted(p for p in inbox.glob("*") if p.suffix in (".zip", ".xml"))
