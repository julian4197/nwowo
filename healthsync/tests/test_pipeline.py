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
