"""Motivation and progress dashboard, served by the container on the HAE port.

GET /                     the page (phone-first, can be added to the home screen)
GET /api/dashboard.json   all numbers the page shows

Everything is computed from the local database and the last plan.json, so the
page works without internet. Goals come from the plan file::

    {"goals": {
       "races": [{"name": "Silvesterlauf", "date": "2026-12-31",
                  "distance_km": 10.46, "target": "54:23", "previous": "54:23"}],
       "strength_per_week": 2,
       "protein_g_per_kg": 1.6},
     "workouts": [...]}
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from . import store, summary

RUN = {"running", "trail_running", "treadmill_running", "track_running", "street_running", "virtual_run"}
BIKE = {"cycling", "indoor_cycling", "virtual_ride", "road_biking", "mountain_biking", "gravel_cycling"}
STRENGTH = {"strength_training", "hiit", "indoor_cardio", "cardio_training"}
RACKET = {"badminton", "tennis", "squash", "table_tennis", "padel", "racket_sports"}

RIEGEL_EXP = 1.06  # Riegel 1981, Am Sci 69:285


def category(type_key: str | None) -> str:
    t = (type_key or "").lower()
    if t in RUN or "run" in t:
        return "running"
    if t in BIKE or "cycl" in t or "bik" in t:
        return "cycling"
    if t in STRENGTH or "strength" in t:
        return "strength"
    if t in RACKET:
        return "racket"
    if "walk" in t or "hik" in t:
        return "walking"
    return "other"


def hms(s: str | float) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    total = 0.0
    for p in str(s).split(":"):
        total = total * 60 + float(p)
    return total


def fmt_time(sec: float | None) -> str | None:
    if sec is None:
        return None
    sec = int(round(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def load_activities(con: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    out = []
    for day, type_key, payload in con.execute(
        "SELECT day, type, payload FROM activities WHERE day >= ? ORDER BY day", (since,)
    ):
        a = json.loads(payload)
        dist = a.get("distance") or 0
        dur = a.get("movingDuration") or a.get("duration") or 0
        out.append({
            "day": day,
            "sport": category(type_key),
            "name": a.get("activityName") or type_key,
            "km": round(dist / 1000, 2),
            "min": round(dur / 60, 1),
            "sec": dur,
            "hr": a.get("averageHR"),
            "split_5k": a.get("fastestSplit_5000"),
        })
    return out


def match_plan(planned: list[dict[str, Any]], acts: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Mark each planned session done/missed/open.

    Same sport on the same day counts first, then the same sport anywhere in the
    same week (Juli shifts sessions around the calendar). Badminton fulfils a
    planned bike session.
    """
    def fits(plan_sport: str, act_sport: str) -> bool:
        return act_sport == plan_sport or (plan_sport == "cycling" and act_sport == "racket")

    used: set[int] = set()
    result = []
    items = sorted(planned, key=lambda w: w["date"])
    status: dict[str, str] = {}
    for w in items:  # pass 1: same day
        for i, a in enumerate(acts):
            if i not in used and a["day"] == w["date"] and fits(w["sport"], a["sport"]):
                used.add(i)
                status[w["id"]] = "done"
                break
    for w in items:  # pass 2: same week
        if w["id"] in status:
            continue
        ws = week_start(date.fromisoformat(w["date"]))
        for i, a in enumerate(acts):
            if i in used or not fits(w["sport"], a["sport"]):
                continue
            if week_start(date.fromisoformat(a["day"])) == ws:
                used.add(i)
                status[w["id"]] = "done"
                break
    for w in items:
        d = date.fromisoformat(w["date"])
        st = status.get(w["id"])
        if not st:
            if d < today and week_start(d) < week_start(today):
                st = "missed"
            elif d < today:
                st = "late"  # still possible this week
            elif d == today:
                st = "today"
            else:
                st = "upcoming"
        result.append({"id": w["id"], "date": w["date"], "sport": w["sport"], "name": w.get("name"),
                       "description": w.get("description"), "status": st})
    return result


def weeks_overview(acts, plan_items, today: date, n: int = 12) -> list[dict[str, Any]]:
    first = week_start(today) - timedelta(weeks=n - 1)
    weeks = []
    for k in range(n):
        ws = first + timedelta(weeks=k)
        we = ws + timedelta(days=6)
        in_week = [a for a in acts if ws.isoformat() <= a["day"] <= we.isoformat()]
        planned = [p for p in plan_items if ws.isoformat() <= p["date"] <= we.isoformat()]
        sessions = [a for a in in_week if a["sport"] != "walking"]
        weeks.append({
            "start": ws.isoformat(),
            "run_km": round(sum(a["km"] for a in in_week if a["sport"] == "running"), 1),
            "sessions": len(sessions),
            "strength": sum(1 for a in in_week if a["sport"] == "strength"),
            "minutes": round(sum(a["min"] for a in sessions)),
            "planned": len(planned),
            "planned_done": sum(1 for p in planned if p["status"] == "done"),
            "current": ws == week_start(today),
        })
    return weeks


def week_met(w: dict[str, Any]) -> bool:
    if w["planned"]:
        return w["planned_done"] / w["planned"] >= 0.8
    return w["sessions"] >= 3


def streak(weeks: list[dict[str, Any]]) -> int:
    """Weeks in a row with the plan (>= 80 %) or >= 3 sessions done.

    The running week counts once it is met; if it is not met yet it does not
    break the streak.
    """
    n = 0
    for w in reversed(weeks):
        if week_met(w):
            n += 1
        elif not w["current"]:
            break
    return n


def predict(acts, distance_km: float, today: date, window_days: int = 56,
            min_hr: float = 170) -> dict[str, Any] | None:
    """Best Riegel prediction from hard runs (avg HR >= ``min_hr``) of the last ``window_days``.

    Easy runs would predict a far too slow race time, so they are ignored.
    """
    since = (today - timedelta(days=window_days)).isoformat()
    best = None
    for a in acts:
        if a["sport"] != "running" or a["day"] < since or (a.get("hr") or 0) < min_hr:
            continue
        cands = []
        if a.get("split_5k"):
            cands.append((5.0, float(a["split_5k"])))
        if a["km"] >= 3 and a["sec"]:
            cands.append((a["km"], float(a["sec"])))
        for d, t in cands:
            pred = t * (distance_km / d) ** RIEGEL_EXP
            if best is None or pred < best["sec"]:
                best = {"sec": pred, "from_day": a["day"], "from_km": d, "from_time": fmt_time(t)}
    if best:
        best["time"] = fmt_time(best["sec"])
        best["pace"] = fmt_time(best["sec"] / distance_km)
    return best


def _series(days, metric, start: date, end: date) -> list[list[Any]]:
    out = []
    d = start
    while d <= end:
        v = days.get(d.isoformat(), {}).get(metric)
        out.append([d.isoformat(), v if isinstance(v, (int, float)) else None])
        d += timedelta(days=1)
    return out


def _rolling(series, n=7) -> list[list[Any]]:
    out = []
    for i, (d, _) in enumerate(series):
        vals = [v for _, v in series[max(0, i - n + 1): i + 1] if v is not None]
        out.append([d, round(mean(vals), 2) if vals else None])
    return out


def _last(series):
    for _, v in reversed(series):
        if v is not None:
            return v
    return None


def ampel(flags: list[str], latest: dict[str, Any]) -> str:
    if len(flags) >= 2:
        return "rot"
    if flags:
        return "gelb"
    if not latest:
        return "grau"
    return "grün"


def build(con: sqlite3.Connection, plan: dict[str, Any] | None, today: date) -> dict[str, Any]:
    plan = plan or {}
    goals = plan.get("goals") or {}
    days = store.load_daily(con, (today - timedelta(days=120)).isoformat())
    acts = load_activities(con, (today - timedelta(days=400)).isoformat())
    recent_acts = [a for a in acts if a["day"] >= (today - timedelta(days=120)).isoformat()]
    plan_items = match_plan(plan.get("workouts") or [], recent_acts, today)

    s = summary.build(days, today)
    latest = days.get(s["latest_day"] or "", {})
    flags = s["flags"]
    weeks = weeks_overview(recent_acts, plan_items, today)
    cur = weeks[-1]

    # Recovery: last 28 days with 7d vs 28d averages.
    r_start = today - timedelta(days=27)
    recovery = {}
    for m, label, unit, good in [
        ("hrv_last_night", "HRV", "ms", "up"),
        ("resting_hr", "Ruhepuls", "bpm", "down"),
        ("sleep_min", "Schlaf", "min", "up"),
        ("body_battery_high", "Body Battery", "", "up"),
    ]:
        ser = _series(days, m, r_start, today)
        recovery[m] = {"label": label, "unit": unit, "good": good, "series": ser,
                       "latest": _last(ser), **{k: v for k, v in s["baselines"].get(m, {}).items() if k != "latest"}}
    recovery["hrv_last_night"]["baseline"] = [latest.get("hrv_baseline_low"), latest.get("hrv_baseline_high")]

    # Body: weight 90 days, protein vs target.
    w_ser = _series(days, "weight_kg", today - timedelta(days=89), today)
    w_avg = _rolling(w_ser)
    w_now = _last(w_avg)
    w_4w = next((v for d, v in reversed(w_avg) if d <= (today - timedelta(days=28)).isoformat() and v), None)
    protein_7d = s["baselines"].get("protein_g", {}).get("avg_7d")
    kcal_7d = s["baselines"].get("kcal_in", {}).get("avg_7d")
    g_per_kg = goals.get("protein_g_per_kg", 1.6)
    last_weigh = next((d for d, v in reversed(w_ser) if v is not None), None)

    # Races.
    races = []
    for r in goals.get("races") or []:
        rd = date.fromisoformat(r["date"])
        if rd < today:
            continue
        target = hms(r["target"])
        pred = predict(acts, r["distance_km"], today, min_hr=goals.get("effort_hr_min", 170))
        races.append({
            "name": r["name"], "date": r["date"], "days_left": (rd - today).days,
            "distance_km": r["distance_km"], "target": fmt_time(target),
            "target_pace": fmt_time(target / r["distance_km"]),
            "previous": r.get("previous"), "prediction": pred,
            "gap_sec": round(pred["sec"] - target) if pred else None,
            "date_tentative": bool(r.get("date_tentative")),
        })

    # Race prep progress: planned sessions up to the race, how many are done.
    for r in races:
        until = [p for p in plan_items if p["date"] <= r["date"]]
        r["plan_total"] = len(until)
        r["plan_done"] = sum(p["status"] == "done" for p in until)
        r["plan_start"] = min((p["date"] for p in until), default=None)

    ws_today = week_start(today)
    week_days = [ws_today + timedelta(days=i) for i in range(7)]
    intensity = 0
    for d in week_days:
        m = days.get(d.isoformat(), {})
        intensity += (m.get("intensity_min_moderate") or 0) + 2 * (m.get("intensity_min_vigorous") or 0)
    week_daily = []
    for d in week_days:
        ds = d.isoformat()
        day_acts = [a for a in recent_acts if a["day"] == ds and a["sport"] != "walking"]
        week_daily.append({"day": ds, "min": round(sum(a["min"] for a in day_acts)),
                           "run_km": round(sum(a["km"] for a in day_acts if a["sport"] == "running"), 1),
                           "sports": sorted({a["sport"] for a in day_acts}),
                           "planned": [p["sport"] for p in plan_items if p["date"] == ds]})

    year_weeks = weeks_overview(acts, plan_items, today, n=52)
    records = records_of(acts, year_weeks)
    plan_start = min((p["date"] for p in plan_items), default=None)
    since_plan = [a for a in acts if plan_start and a["day"] >= plan_start]
    badges = badges_of(plan_items, year_weeks, since_plan, races, goals, acts)

    upcoming = [p for p in plan_items if p["status"] in ("today", "upcoming", "late")]
    today_items = [p for p in plan_items if p["date"] == today.isoformat()]
    year_start = date(today.year, 1, 1).isoformat()
    total_planned = [p for p in plan_items if p["status"] in ("done", "missed")]

    return {
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "name": goals.get("name") or os.getenv("DASHBOARD_NAME"),
        "today": today.isoformat(),
        "latest_day": s["latest_day"],
        "ampel": ampel(flags, latest),
        "flags": flags,
        "readiness": latest.get("training_readiness"),
        "today_plan": today_items,
        "next": upcoming[:3],
        "races": races,
        "week": {
            "planned": cur["planned"], "done": cur["planned_done"], "sessions": cur["sessions"],
            "run_km": cur["run_km"], "strength": cur["strength"],
            "strength_target": goals.get("strength_per_week", 2),
            "intensity_min": intensity, "intensity_target": goals.get("intensity_min_per_week", 150),
            "days": week_daily,
            "run_km_avg4": round(mean(w["run_km"] for w in weeks[-5:-1]), 1),
        },
        "records": records,
        "badges": badges,
        "streak_best": best_streak(year_weeks),
        "streak_dots": [week_met(w) for w in year_weeks[-8:]],
        "streak_weeks": streak(year_weeks),
        "adherence_pct": round(100 * sum(p["status"] == "done" for p in total_planned) / len(total_planned))
        if total_planned else None,
        "weeks": weeks,
        "plan": plan_items,
        "heatmap": [[a["day"], a["sport"], a["km"], a["min"]] for a in recent_acts
                    if a["day"] >= (week_start(today) - timedelta(weeks=11)).isoformat()],
        "year": {
            "run_km": round(sum(a["km"] for a in acts if a["sport"] == "running" and a["day"] >= year_start)),
            "sessions": sum(1 for a in acts if a["sport"] != "walking" and a["day"] >= year_start),
        },
        "recent": [a for a in reversed(recent_acts)][:10],
        "recovery": recovery,
        "body": {
            "weight": w_ser, "weight_avg": w_avg, "weight_now": w_now,
            "weight_change_4w": round(w_now - w_4w, 1) if w_now and w_4w else None,
            "last_weigh": last_weigh,
            "protein_7d": protein_7d, "kcal_7d": kcal_7d,
            "protein_target": round(g_per_kg * w_now) if w_now else None, "protein_g_per_kg": g_per_kg,
        },
    }


def best_streak(weeks: list[dict[str, Any]]) -> int:
    best = cur = 0
    for w in weeks:
        cur = cur + 1 if week_met(w) else (cur if w["current"] else 0)
        best = max(best, cur)
    return best


def records_of(acts, weeks) -> dict[str, Any]:
    runs = [a for a in acts if a["sport"] == "running" and a["km"] > 0]
    longest = max(runs, key=lambda a: a["km"], default=None)
    splits = [a for a in runs if a.get("split_5k")]
    fastest = min(splits, key=lambda a: a["split_5k"], default=None)
    best_week = max(weeks, key=lambda w: w["run_km"], default=None)
    most = max(weeks, key=lambda w: w["sessions"], default=None)
    return {
        "longest_run": {"km": longest["km"], "day": longest["day"]} if longest else None,
        "fastest_5k": {"time": fmt_time(fastest["split_5k"]), "day": fastest["day"]} if fastest else None,
        "best_week_km": {"km": best_week["run_km"], "week": best_week["start"]} if best_week and best_week["run_km"] else None,
        "most_sessions": {"n": most["sessions"], "week": most["start"]} if most and most["sessions"] else None,
    }


def badges_of(plan_items, weeks, since_plan, races, goals, acts) -> list[dict[str, Any]]:
    """Milestones with progress 0..1, ordered: almost reached first."""
    strk = streak(weeks)
    perfect = any(w["planned"] and w["planned_done"] >= w["planned"] for w in weeks)
    km = sum(a["km"] for a in since_plan if a["sport"] == "running")
    strength = sum(1 for a in since_plan if a["sport"] == "strength")
    hard = [a for a in since_plan if a["sport"] == "running" and (a.get("hr") or 0) >= goals.get("effort_hr_min", 170)]
    out = [
        {"icon": "⭐", "title": "Perfekte Woche", "desc": "Alle geplanten Einheiten einer Woche", "progress": 1.0 if perfect else
         max((w["planned_done"] / w["planned"] for w in weeks if w["planned"]), default=0)},
        {"icon": "🔥", "title": "4 Wochen am Stück", "desc": "Vier Wochen in Folge geschafft", "progress": min(1, strk / 4)},
        {"icon": "🚀", "title": "12 Wochen am Stück", "desc": "Eine echte Routine", "progress": min(1, strk / 12)},
        {"icon": "🛣️", "title": "100 km", "desc": "Gelaufen seit Planstart", "progress": min(1, km / 100), "value": round(km)},
        {"icon": "💪", "title": "10 × Kraft", "desc": "Krafteinheiten seit Planstart", "progress": min(1, strength / 10),
         "value": strength},
        {"icon": "⚡", "title": "Harter Lauf", "desc": "Erster Lauf mit Ø-Puls über "
         f"{goals.get('effort_hr_min', 170)}, z. B. der 5-km-Test", "progress": 1.0 if hard else 0},
    ]
    for r in goals.get("races") or []:
        target = hms(r["target"])
        done = any(a["sport"] == "running" and a["day"] == r["date"] and a["km"] >= r["distance_km"] * 0.95
                   and a["sec"] and a["sec"] * (r["distance_km"] / a["km"]) <= target for a in acts)
        out.append({"icon": "🏆", "title": f"{r['name']} < {fmt_time(target)}", "desc": "Das große Ziel",
                    "progress": 1.0 if done else 0})
    for b in out:
        b["progress"] = round(b["progress"], 2)
        b["achieved"] = b["progress"] >= 1
    return out


def load_plan(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def page() -> bytes:
    return (Path(__file__).parent / "dashboard.html").read_bytes()


def manifest() -> bytes:
    return json.dumps({
        "name": "Coach", "short_name": "Coach", "start_url": "./", "display": "standalone",
        "background_color": "#121211", "theme_color": "#e0662b",
        "icons": [{"src": "icon.png", "sizes": "180x180", "type": "image/png"}],
    }).encode()


def icon_png(size: int = 180) -> bytes:
    """Orange tile with a white progress ring, drawn without image libraries."""
    import math
    import struct
    import zlib

    c, r_out, r_in = size / 2, size * 0.34, size * 0.24
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            dx, dy = x - c + 0.5, y - c + 0.5
            dist = (dx * dx + dy * dy) ** 0.5
            ang = (math.degrees(math.atan2(dx, -dy)) + 360) % 360
            on_ring = r_in <= dist <= r_out
            if on_ring and ang <= 270:
                row += bytes((255, 255, 255))
            elif on_ring:
                row += bytes((240, 160, 120))
            else:
                row += bytes((224, 102, 43))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))
