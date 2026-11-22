"""Receive pushes from the iOS app "Health Auto Export" (REST API automation).

The app POSTs JSON like
  {"data": {"metrics": [{"name": "weight_body_mass", "units": "kg",
                         "data": [{"date": "2026-11-22 07:12:00 +0100", "qty": 81.4}]}]}}
We keep weight/body composition (last value of the day) and nutrition
(sum per day) and store them as source "hae".
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import store

log = logging.getLogger(__name__)

# metric name in the app -> (our field, how to combine values within a day)
METRICS = {
    "weight_body_mass": ("weight_kg", "last"),
    "body_fat_percentage": ("body_fat_pct", "last"),
    "lean_body_mass": ("lean_mass_kg", "last"),
    "body_mass_index": ("bmi", "last"),
    "dietary_energy": ("kcal_in", "sum"),
    "protein": ("protein_g", "sum"),
    "carbohydrates": ("carbs_g", "sum"),
    "total_fat": ("fat_g", "sum"),
    "fiber": ("fiber_g", "sum"),
    "dietary_water": ("water_ml", "sum"),
}

UNIT_FACTORS = {
    ("kcal_in", "kj"): 1 / 4.184,
    ("weight_kg", "lb"): 0.45359237,
    ("lean_mass_kg", "lb"): 0.45359237,
    ("water_ml", "l"): 1000,
    ("water_ml", "fl_oz_us"): 29.5735,
}


def parse(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return {day: {field: value}} from one Health Auto Export payload."""
    metrics = (payload.get("data") or {}).get("metrics") or []
    per_day: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        spec = METRICS.get(metric.get("name"))
        if not spec:
            continue
        field, how = spec
        factor = UNIT_FACTORS.get((field, str(metric.get("units", "")).lower()), 1)
        points = sorted(
            (p for p in metric.get("data") or [] if isinstance(p.get("qty"), (int, float))),
            key=lambda p: str(p.get("date", "")),
        )
        for p in points:
            day = str(p.get("date", ""))[:10]
            if len(day) != 10:
                continue
            value = p["qty"] * factor
            if field == "body_fat_pct" and value <= 1:  # app may send a fraction
                value *= 100
            row = per_day.setdefault(day, {})
            row[field] = value if how == "last" or field not in row else row[field] + value
    for row in per_day.values():
        for k, v in row.items():
            row[k] = round(v, 2)
    return per_day


def ingest(db_path: str, payload: dict[str, Any]) -> list[str]:
    con = store.connect(db_path)
    days = parse(payload)
    for day, fields in days.items():
        existing = store.load_source_day(con, day, "hae")
        existing.update(fields)
        store.save_daily(con, day, "hae", existing)
    con.close()
    return sorted(days)


def serve(db_path: str, port: int, token: str | None, on_update=None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            if token and self.headers.get("X-Token") != token:
                self.send_response(401)
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                days = ingest(db_path, json.loads(self.rfile.read(length) or b"{}"))
            except Exception as e:
                log.warning("Health Auto Export push rejected: %s", e)
                self.send_response(400)
                self.end_headers()
                return
            log.info("Health Auto Export: %d days received", len(days))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            if on_update and days:
                threading.Thread(target=on_update, daemon=True).start()

        def log_message(self, *args):  # keep container logs quiet
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Listening for Health Auto Export on port %d", port)
    return server


def enabled() -> bool:
    return bool(os.getenv("HAE_PORT"))
