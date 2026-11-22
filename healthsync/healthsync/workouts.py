"""Push the training plan to Garmin Connect as scheduled workouts.

The plan is a JSON file (``plan.json``), written by the coach::

    {"workouts": [
      {"id": "2026-11-24-intervals", "date": "2026-11-24", "sport": "running",
       "name": "5x1000 m", "description": "...",
       "steps": [
         {"type": "warmup", "time": "15:00", "target": {"hr_zone": 2}},
         {"type": "repeat", "times": 5, "steps": [
            {"type": "interval", "distance_m": 1000, "target": {"pace": "4:50-5:00"}},
            {"type": "recovery", "time": "2:00"}]},
         {"type": "cooldown", "time": "10:00"}]},
      {"id": "2026-11-25-kraft-a", "date": "2026-11-25", "sport": "strength",
       "name": "Kraft A",
       "steps": [
         {"exercise": "Goblet Squat", "sets": 3, "reps": 10, "rest": "1:30", "weight_kg": 18},
         {"exercise": "Plank", "sets": 3, "time": "0:45", "rest": "0:45"}]}
    ]}

Running steps: ``type`` warmup | interval | recovery | cooldown | rest | repeat.
Duration: ``time`` ("mm:ss" or seconds), ``distance_m``, or neither (lap button).
Target: ``pace`` "m:ss-m:ss" per km, ``hr`` [low, high] bpm, ``hr_zone`` 1-5.

Strength steps: ``exercise`` is a Garmin display name (e.g. "Goblet Squat")
or "CATEGORY/EXERCISE"; ``reps`` or ``time``; ``sets``; ``rest``; optional
``weight_kg`` and ``note``.

Workouts this tool created are remembered by ``id``. A changed workout is
replaced, a workout removed from the plan is deleted from Garmin (only future
ones). Workouts made by hand in Garmin Connect are never touched.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import date
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_workouts (
    plan_id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    hash TEXT NOT NULL,
    workout_id TEXT NOT NULL,
    pushed_at TEXT NOT NULL
);
"""

SPORTS = {
    "running": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "strength": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5},
    "cardio": {"sportTypeId": 6, "sportTypeKey": "cardio_training", "displayOrder": 6},
    "mobility": {"sportTypeId": 11, "sportTypeKey": "mobility", "displayOrder": 11},
}
STEP_TYPES = {
    "warmup": (1, 1),
    "cooldown": (2, 2),
    "interval": (3, 3),
    "recovery": (4, 4),
    "rest": (5, 5),
}
NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}


def seconds(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    parts = [float(p) for p in str(v).split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def pace_mps(pace: str) -> float:
    """'5:00' per km -> metres per second."""
    return 1000.0 / seconds(pace)


def _target(t: dict[str, Any] | None) -> dict[str, Any]:
    if not t:
        return {"targetType": NO_TARGET}
    if "pace" in t:
        fast, slow = sorted((p.strip() for p in str(t["pace"]).split("-")), key=seconds)
        # Garmin wants the slower pace (lower speed) first.
        return {
            "targetType": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6},
            "targetValueOne": round(pace_mps(slow), 4),
            "targetValueTwo": round(pace_mps(fast), 4),
        }
    hr_type = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4}
    if "hr_zone" in t:
        return {"targetType": hr_type, "zoneNumber": int(t["hr_zone"])}
    if "hr" in t:
        lo, hi = t["hr"]
        return {"targetType": hr_type, "targetValueOne": float(lo), "targetValueTwo": float(hi)}
    raise ValueError(f"unknown target {t}")


def _end(step: dict[str, Any]) -> dict[str, Any]:
    if "distance_m" in step:
        return {
            "endCondition": {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True},
            "endConditionValue": float(step["distance_m"]),
        }
    if "time" in step:
        return {
            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
            "endConditionValue": seconds(step["time"]),
        }
    return {"endCondition": {"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True}}


class _Order:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def _repeat(order: int, times: int, steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "RepeatGroupDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
        "numberOfIterations": int(times),
        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
        "endConditionValue": float(times),
        "smartRepeat": False,
        "workoutSteps": steps,
    }


def _cardio_step(step: dict[str, Any], order: _Order) -> dict[str, Any]:
    kind = step.get("type", "interval")
    if kind == "repeat":
        o = order.next()
        return _repeat(o, step["times"], [_cardio_step(s, order) for s in step["steps"]])
    type_id, display = STEP_TYPES[kind]
    out = {
        "type": "ExecutableStepDTO",
        "stepOrder": order.next(),
        "stepType": {"stepTypeId": type_id, "stepTypeKey": kind, "displayOrder": display},
        **_end(step),
        **_target(step.get("target")),
    }
    if step.get("note"):
        out["description"] = step["note"]
    return out


def _exercise(name: str) -> tuple[str, str]:
    if "/" in name:
        cat, ex = name.split("/", 1)
        return cat, ex
    from garminconnect import exercises

    entry = exercises.resolve(name)
    if not entry:
        hits = exercises.find(name)
        if not hits:
            raise ValueError(f"Garmin kennt die Übung '{name}' nicht")
        entry = hits[0]
    return entry["category"], entry["exercise"]


def _strength_block(step: dict[str, Any], order: _Order) -> dict[str, Any]:
    group_order = order.next()
    category, exercise = _exercise(step["exercise"])
    ex: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order.next(),
        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
        "targetType": NO_TARGET,
        "category": category,
        "exerciseName": exercise,
    }
    if "reps" in step:
        ex["endCondition"] = {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True}
        ex["endConditionValue"] = float(step["reps"])
    else:
        ex.update(_end(step))
    if step.get("weight_kg") is not None:
        ex["weightValue"] = float(step["weight_kg"]) * 1000.0
        ex["weightUnit"] = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
    if step.get("note"):
        ex["description"] = step["note"]
    inner = [ex]
    if step.get("rest"):
        inner.append({
            "type": "ExecutableStepDTO",
            "stepOrder": order.next(),
            "stepType": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
            **_end({"time": step["rest"]}),
            "targetType": NO_TARGET,
        })
    group = _repeat(group_order, step.get("sets", 1), inner)
    group["skipLastRestStep"] = True
    return group


def build(w: dict[str, Any]) -> dict[str, Any]:
    """Plan entry -> Garmin workout JSON."""
    sport = SPORTS[w.get("sport", "running")]
    order = _Order()
    if sport["sportTypeKey"] == "strength_training":
        steps = [_strength_block(s, order) for s in w["steps"]]
    else:
        steps = [_cardio_step(s, order) for s in w["steps"]]
    out = {
        "workoutName": w["name"][:80],
        "sportType": sport,
        "estimatedDurationInSecs": int(seconds(w.get("duration", 0))),
        "workoutSegments": [{"segmentOrder": 1, "sportType": sport, "workoutSteps": steps}],
    }
    if w.get("description"):
        out["description"] = w["description"][:1000]
    return out


def _hash(w: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(w, sort_keys=True).encode()).hexdigest()[:16]


def push(con: sqlite3.Connection, client, plan: dict[str, Any], today: date | None = None) -> dict[str, int]:
    """Make Garmin match the plan. Returns counts of created/updated/deleted/unchanged."""
    con.executescript(SCHEMA)
    today_s = (today or date.today()).isoformat()
    stats = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0}
    known = {
        r[0]: {"hash": r[1], "workout_id": r[2], "day": r[3]}
        for r in con.execute("SELECT plan_id, hash, workout_id, day FROM plan_workouts")
    }
    wanted = {w["id"]: w for w in plan.get("workouts", []) if w.get("date", "") >= today_s}

    for pid, row in known.items():
        if pid not in wanted and row["day"] >= today_s:
            _delete(client, row["workout_id"])
            con.execute("DELETE FROM plan_workouts WHERE plan_id = ?", (pid,))
            stats["deleted"] += 1

    for pid, w in wanted.items():
        h = _hash(w)
        old = known.get(pid)
        if old and old["hash"] == h:
            stats["unchanged"] += 1
            continue
        if old:
            _delete(client, old["workout_id"])
        created = client.upload_workout(build(w))
        wid = str(created["workoutId"])
        client.schedule_workout(wid, w["date"])
        con.execute(
            "INSERT OR REPLACE INTO plan_workouts VALUES (?, ?, ?, ?, datetime('now'))",
            (pid, w["date"], h, wid),
        )
        stats["updated" if old else "created"] += 1
        log.info("Garmin workout %s: %s (%s)", "updated" if old else "created", w["name"], w["date"])
    con.commit()
    return stats


def _delete(client, workout_id: str) -> None:
    try:
        client.delete_workout(workout_id)  # also removes it from the calendar
    except Exception as e:
        log.warning("Could not delete Garmin workout %s: %s", workout_id, e)
