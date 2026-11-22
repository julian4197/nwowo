import json
from datetime import date, timedelta

from healthsync import dashboard, healthfit, store

TODAY = date(2026, 11, 26)  # Thursday


def _act(i, day, type_key, km, minutes, split=None):
    return {"activityId": i, "startTimeLocal": f"{day} 07:00:00", "activityType": {"typeKey": type_key},
            "activityName": type_key, "distance": km * 1000, "duration": minutes * 60,
            "movingDuration": minutes * 60, "averageHR": 175 if split else 150, "fastestSplit_5000": split}


def _db(tmp_path):
    con = store.connect(str(tmp_path / "h.db"))
    for i in range(40):
        d = (TODAY - timedelta(days=i)).isoformat()
        store.save_daily(con, d, "garmin", {"resting_hr": 50, "hrv_last_night": 50, "sleep_min": 450,
                                            "body_battery_high": 80, "hrv_baseline_low": 45,
                                            "hrv_baseline_high": 60})
        store.save_daily(con, d, "healthfit", {"weight_kg": 80 - i * 0.02, "protein_g": 120})
    acts = [
        _act(1, "2026-11-23", "running", 5, 33),            # Mo planned run
        _act(2, "2026-11-25", "strength_training", 0, 35),  # Kraft A done on Wed instead of Mon
        _act(3, "2026-11-24", "badminton", 0, 90),          # replaces bike
        _act(4, "2026-11-12", "running", 8, 50, split=1500),
    ]
    for w in range(1, 6):  # earlier weeks: 3 sessions each
        mon = TODAY - timedelta(days=TODAY.weekday() + 7 * w)
        for k in range(3):
            acts.append(_act(100 + w * 10 + k, (mon + timedelta(days=k)).isoformat(), "running", 6, 40))
    store.save_activities(con, acts)
    return con


PLAN = {
    "goals": {"races": [{"name": "Silvesterlauf", "date": "2026-12-31", "distance_km": 10.46,
                         "target": "54:23"}], "strength_per_week": 2},
    "workouts": [
        {"id": "a", "date": "2026-11-23", "sport": "running", "name": "Locker"},
        {"id": "b", "date": "2026-11-23", "sport": "strength", "name": "Kraft A"},
        {"id": "c", "date": "2026-11-24", "sport": "cycling", "name": "Rad"},
        {"id": "d", "date": "2026-11-25", "sport": "running", "name": "Steigerungen"},
        {"id": "e", "date": "2026-11-26", "sport": "strength", "name": "Kraft B"},
        {"id": "f", "date": "2026-11-29", "sport": "running", "name": "Lang"},
    ],
}


def test_build(tmp_path):
    con = _db(tmp_path)
    d = dashboard.build(con, PLAN, TODAY)
    json.dumps(d)
    st = {p["id"]: p["status"] for p in d["plan"]}
    assert st == {"a": "done", "b": "done", "c": "done", "d": "late", "e": "today", "f": "upcoming"}
    assert d["week"]["planned"] == 6 and d["week"]["done"] == 3
    assert d["streak_weeks"] >= 5  # current week not met yet, does not break the streak
    assert d["ampel"] == "grün"
    race = d["races"][0]
    assert race["days_left"] == 35 and race["target_pace"] == "5:12"
    # 5 km split in 25:00 -> Riegel to 10.46 km ~ 54:22
    assert race["prediction"]["time"].startswith("54:")
    assert d["body"]["protein_target"] == 128
    assert d["today_plan"][0]["name"] == "Kraft B"


def test_build_without_plan_or_data(tmp_path):
    con = store.connect(str(tmp_path / "e.db"))
    d = dashboard.build(con, None, TODAY)
    assert d["ampel"] == "grau" and d["races"] == [] and d["streak_weeks"] == 0


def test_healthfit_rows():
    rows = [("Datum", "Gewicht", "Quelle", "Fett", "Quelle", "BMI", "Quelle"),
            ("27.08.2026", "80,05 kg", "Zepp Life", "20%", "Zepp Life", "22,4", "Zepp Life"),
            ("14.07.2026", 78.95, "Zepp Life", 0.2, None, None, None),
            (None, None)]
    out = healthfit.parse_rows("Gewicht", rows)
    assert out["2026-08-27"] == {"weight_kg": 80.05, "body_fat_pct": 20.0, "bmi": 22.4}
    assert out["2026-07-14"] == {"weight_kg": 78.95, "body_fat_pct": 20.0}
    food = healthfit.parse_rows("Ernährung", [("Datum", "Wasser", "Quelle", "Nahrungsenergie", "Quelle",
                                               "Gesamtfettanteil", "Quelle", "Protein"),
                                              ("07.08.2026", "2,1 Tassen", "Yazio", "1577 kcal", "Yazio",
                                               "72 g", "Yazio", "88 g")])
    assert food["2026-08-07"] == {"kcal_in": 1577.0, "fat_g": 72.0, "protein_g": 88.0}


def test_dashboard_http(tmp_path):
    import urllib.request
    from healthsync import hae

    _db(tmp_path).close()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN))
    srv = hae.serve(str(tmp_path / "h.db"), 0, "t", plan_path=str(plan))
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    assert b"<title>Coach</title>" in urllib.request.urlopen(base + "/").read()
    data = json.loads(urllib.request.urlopen(base + "/api/dashboard.json").read())
    assert data["races"][0]["name"] == "Silvesterlauf"
    assert urllib.request.urlopen(base + "/icon.png").read()[:4] == b"\x89PNG"
    srv.shutdown()
