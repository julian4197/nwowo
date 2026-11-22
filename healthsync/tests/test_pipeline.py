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
