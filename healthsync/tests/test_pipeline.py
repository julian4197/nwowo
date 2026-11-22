import json
from datetime import date, timedelta

from healthsync import garmin, store, summary

RAW = {
    "summary": {"restingHeartRate": 52, "totalSteps": 9000, "averageStressLevel": 30,
                "bodyBatteryHighestValue": 45},
    "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 5 * 3600, "deepSleepSeconds": 3600,
                                "sleepScores": {"overall": {"value": 61}}}},
    "hrv": {"hrvSummary": {"lastNightAvg": 38, "weeklyAvg": 45, "status": "UNBALANCED",
                           "baseline": {"balancedLow": 42, "balancedUpper": 55}}},
    "readiness": [{"score": 33, "level": "LOW"}],
    "heart_rates": {
        "startTimestampGMT": "2026-11-21T23:00:00.0",
        "startTimestampLocal": "2026-11-22T00:00:00.0",
        # 2026-11-21T23:00Z and 23:30Z -> local hour 00
        "heartRateValues": [[1795302000000, 50], [1795303800000, 54], [1795305600000, None]],
    },
    "body": {"dateWeightList": [{"weight": 81700, "bodyFat": 18.5}]},
}


def test_flatten():
    m = garmin.flatten(RAW)
    assert m["resting_hr"] == 52
    assert m["sleep_min"] == 300.0
    assert m["sleep_score"] == 61
    assert m["hrv_last_night"] == 38
    assert m["training_readiness"] == 33
    assert m["weight_kg"] == 81.7
    assert m["hr_hourly"] == {"00": 52}


def test_flatten_handles_missing_data():
    m = garmin.flatten({k: None for k in RAW})
    assert m["resting_hr"] is None and m["hr_hourly"] == {}


def test_store_and_summary(tmp_path):
    con = store.connect(str(tmp_path / "h.db"))
    today = date(2026, 11, 22)
    for i in range(20):
        d = (today - timedelta(days=i)).isoformat()
        m = garmin.flatten(RAW)
        m["resting_hr"] = 46 if i else 52
        store.save_raw(con, d, "garmin", RAW)
        store.save_daily(con, d, "garmin", m)
    store.save_daily(con, today.isoformat(), "healthfit", {"weight_kg": 99, "kcal_in": 2500})

    days = store.load_daily(con, "2026-01-01")
    assert days[today.isoformat()]["weight_kg"] == 81.7  # garmin first, gaps filled only
    assert days[today.isoformat()]["kcal_in"] == 2500

    s = summary.build(days, today)
    json.dumps(s)
    assert s["latest_day"] == "2026-11-22"
    assert len(s["days"]) == 14
    assert "hr_hourly" not in s["days"]["2026-11-10"]
    assert "hr_hourly" in s["days"]["2026-11-22"]
    assert s["baselines"]["resting_hr"]["avg_7d"] == round((52 + 6 * 46) / 7, 1)
    assert len(s["flags"]) == 4
    assert summary.to_csv(days).splitlines()[0].startswith("day,resting_hr")


def test_next_run():
    from datetime import datetime
    from healthsync.__main__ import next_run

    now = datetime(2026, 11, 22, 14, 0)
    assert next_run(now, ["07:30", "13:00", "21:30"]) == datetime(2026, 11, 22, 21, 30)
    assert next_run(datetime(2026, 11, 22, 22, 0), ["07:30"]) == datetime(2026, 11, 23, 7, 30)


def test_hae_parse_and_ingest(tmp_path):
    from healthsync import hae

    payload = {"data": {"metrics": [
        {"name": "weight_body_mass", "units": "kg", "data": [
            {"date": "2026-11-22 07:00:00 +0100", "qty": 81.9},
            {"date": "2026-11-22 21:00:00 +0100", "qty": 82.3}]},
        {"name": "body_fat_percentage", "units": "%", "data": [{"date": "2026-11-22 07:00:00 +0100", "qty": 0.2}]},
        {"name": "dietary_energy", "units": "kJ", "data": [
            {"date": "2026-11-22 08:00:00 +0100", "qty": 4184},
            {"date": "2026-11-22 13:00:00 +0100", "qty": 4184}]},
        {"name": "protein", "units": "g", "data": [{"date": "2026-11-21 00:00:00 +0100", "qty": 150}]},
        {"name": "unknown_metric", "data": [{"date": "2026-11-22", "qty": 1}]},
    ]}}
    days = hae.parse(payload)
    assert days["2026-11-22"] == {"weight_kg": 82.3, "body_fat_pct": 20.0, "kcal_in": 2000.0}
    assert days["2026-11-21"] == {"protein_g": 150}

    db = str(tmp_path / "h.db")
    assert hae.ingest(db, payload) == ["2026-11-21", "2026-11-22"]
    hae.ingest(db, {"data": {"metrics": [{"name": "protein", "units": "g",
                                          "data": [{"date": "2026-11-22 09:00:00 +0100", "qty": 90}]}]}})
    merged = store.load_daily(store.connect(db), "2026-11-01")
    assert merged["2026-11-22"]["protein_g"] == 90 and merged["2026-11-22"]["kcal_in"] == 2000.0


def test_hae_http(tmp_path):
    import urllib.request
    from healthsync import hae

    db = str(tmp_path / "h.db")
    srv = hae.serve(db, 0, "secret")
    port = srv.server_address[1]
    body = json.dumps({"data": {"metrics": [{"name": "weight_body_mass", "units": "kg",
                                             "data": [{"date": "2026-11-22 07:00:00 +0100", "qty": 80}]}]}}).encode()
    bad = urllib.request.Request(f"http://127.0.0.1:{port}/", data=body)
    try:
        urllib.request.urlopen(bad)
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 401
    ok = urllib.request.Request(f"http://127.0.0.1:{port}/", data=body, headers={"X-Token": "secret"})
    assert urllib.request.urlopen(ok).status == 200
    srv.shutdown()
    assert store.load_daily(store.connect(db), "2026-11-01")["2026-11-22"]["weight_kg"] == 80


def test_backfill_skips_stored_days_and_saves_activities(tmp_path, monkeypatch):
    import healthsync.__main__ as m

    class Fake:
        class ActivityDownloadFormat:
            ORIGINAL = "orig"
        days: list = []
        def __getattr__(self, name):  # every daily endpoint
            return lambda *a, **k: {}
        def get_user_summary(self, d):
            self.days.append(d)
            return {"restingHeartRate": 50}
        def get_activities_by_date(self, s, e):
            return [{"activityId": 7, "startTimeLocal": "2026-01-02 10:00:00", "activityType": {"typeKey": "running"}}]
        def download_activity(self, i, dl_fmt):
            return b"zip"

    fake = Fake()
    monkeypatch.setattr(m, "DATA_DIR", tmp_path)
    monkeypatch.setattr(m, "DB_PATH", tmp_path / "h.db")
    monkeypatch.setattr(m, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(m, "FIT_DIR", tmp_path / "fit")
    monkeypatch.setattr(m.garmin, "make_client", lambda *a, **k: fake)
    monkeypatch.setenv("RCLONE_REMOTE", "")
    since = date.today() - timedelta(days=2)
    con = store.connect(str(tmp_path / "h.db"))
    store.save_raw(con, since.isoformat(), "garmin", {"summary": {}})
    con.close()
    m.backfill(since, pause=0)
    assert since.isoformat() not in fake.days and len(fake.days) == 2
    assert (tmp_path / "fit" / "7.zip").read_bytes() == b"zip"
    con = store.connect(str(tmp_path / "h.db"))
    assert con.execute("SELECT type FROM activities WHERE id = 7").fetchone() == ("running",)


APPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="de_DE">
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="Zepp Life" unit="kg" startDate="2025-06-01 07:00:00 +0200" endDate="2025-06-01 07:00:00 +0200" value="80.5"/>
 <Record type="HKQuantityTypeIdentifierBodyFatPercentage" sourceName="Zepp Life" unit="%" startDate="2025-06-01 07:00:00 +0200" endDate="2025-06-01 07:00:00 +0200" value="0.21"/>
 <Record type="HKQuantityTypeIdentifierDietaryEnergyConsumed" sourceName="YAZIO" unit="kcal" startDate="2025-06-01 08:00:00 +0200" endDate="2025-06-01 08:00:00 +0200" value="600"/>
 <Record type="HKQuantityTypeIdentifierDietaryEnergyConsumed" sourceName="YAZIO" unit="kcal" startDate="2025-06-01 19:00:00 +0200" endDate="2025-06-01 19:00:00 +0200" value="900"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" startDate="2025-06-01 10:00:00 +0200" endDate="2025-06-01 11:00:00 +0200" value="1200"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Connect" startDate="2025-06-01 00:00:00 +0200" value="HKCategoryValueSleepAnalysisAsleepCore"/>
</HealthData>"""


def test_apple_import(tmp_path):
    import zipfile
    from healthsync import apple

    zpath = tmp_path / "export.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("apple_health_export/export.xml", APPLE_XML)
    db = str(tmp_path / "h.db")
    assert apple.import_export(db, str(zpath)) == 5
    assert apple.import_export(db, str(zpath)) == 5  # re-import does not duplicate
    con = store.connect(db)
    assert con.execute("SELECT COUNT(*) FROM apple_records").fetchone()[0] == 5
    day = store.load_daily(con, "2025-01-01")["2025-06-01"]
    assert day == {"weight_kg": 80.5, "body_fat_pct": 21.0, "kcal_in": 1500.0}


PLAN = {"workouts": [
    {"id": "r1", "date": "2026-11-24", "sport": "running", "name": "5x1000 m",
     "steps": [
         {"type": "warmup", "time": "15:00", "target": {"hr_zone": 2}},
         {"type": "repeat", "times": 5, "steps": [
             {"type": "interval", "distance_m": 1000, "target": {"pace": "5:00-4:50"}},
             {"type": "recovery", "time": 120}]},
         {"type": "cooldown", "time": "10:00", "target": {"hr": [120, 145]}}]},
    {"id": "k1", "date": "2026-11-25", "sport": "strength", "name": "Kraft A",
     "steps": [
         {"exercise": "Goblet Squat", "sets": 3, "reps": 10, "rest": "1:30", "weight_kg": 18, "note": "RIR 2"},
         {"exercise": "PLANK/PLANK", "sets": 3, "time": "0:45", "rest": "0:45"}]},
    {"id": "old", "date": "2026-11-01", "sport": "running", "name": "past", "steps": [{"type": "interval"}]},
]}


def test_build_running():
    from healthsync import workouts as w

    out = w.build(PLAN["workouts"][0])
    steps = out["workoutSegments"][0]["workoutSteps"]
    assert [s["stepOrder"] for s in steps] == [1, 2, 5]
    assert steps[0]["zoneNumber"] == 2
    rep = steps[1]
    assert rep["numberOfIterations"] == 5 and [s["stepOrder"] for s in rep["workoutSteps"]] == [3, 4]
    iv = rep["workoutSteps"][0]
    assert iv["endConditionValue"] == 1000.0
    assert iv["targetValueOne"] == round(1000 / 300, 4) and iv["targetValueTwo"] == round(1000 / 290, 4)
    assert steps[2]["targetValueOne"] == 120.0


def test_build_strength():
    from healthsync import workouts as w

    out = w.build(PLAN["workouts"][1])
    assert out["sportType"]["sportTypeKey"] == "strength_training"
    g = out["workoutSegments"][0]["workoutSteps"][0]
    ex, rest = g["workoutSteps"]
    assert (ex["category"], ex["exerciseName"]) == ("SQUAT", "GOBLET_SQUAT")
    assert ex["endCondition"]["conditionTypeKey"] == "reps" and ex["endConditionValue"] == 10
    assert ex["weightValue"] == 18000 and rest["endConditionValue"] == 90
    plank = out["workoutSegments"][0]["workoutSteps"][1]["workoutSteps"][0]
    assert plank["endCondition"]["conditionTypeKey"] == "time" and plank["endConditionValue"] == 45


class FakeWorkoutClient:
    def __init__(self):
        self.next_id, self.workouts, self.scheduled, self.deleted = 100, {}, {}, []

    def upload_workout(self, payload):
        self.next_id += 1
        self.workouts[self.next_id] = payload
        return {"workoutId": self.next_id}

    def schedule_workout(self, wid, d):
        self.scheduled[wid] = d

    def delete_workout(self, wid):
        self.deleted.append(wid)


def test_push_plan_is_idempotent(tmp_path):
    import copy
    from healthsync import workouts as w

    con = store.connect(str(tmp_path / "h.db"))
    c = FakeWorkoutClient()
    today = date(2026, 11, 22)
    assert w.push(con, c, PLAN, today) == {"created": 2, "updated": 0, "deleted": 0, "unchanged": 0}
    assert c.scheduled == {"101": "2026-11-24", "102": "2026-11-25"}
    assert w.push(con, c, PLAN, today)["unchanged"] == 2
    changed = copy.deepcopy(PLAN)
    changed["workouts"][0]["name"] = "6x1000 m"
    del changed["workouts"][1]
    assert w.push(con, c, changed, today) == {"created": 0, "updated": 1, "deleted": 1, "unchanged": 0}
    assert sorted(c.deleted) == ["101", "102"]
