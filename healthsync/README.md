# healthsync

Holt jeden Tag Schlaf, HRV, Ruhepuls, Puls über den Tag, Stress, Body Battery,
Training Readiness und (falls vorhanden) Gewicht aus Garmin Connect, speichert
alles in einer SQLite-Datenbank auf der NAS und legt eine Tageszusammenfassung
in Google Drive ab. Von dort liest der Coach (Claude) die Daten.

Läuft als ein Docker-Container und berührt keine anderen Container.

## Was entsteht

| Datei | Inhalt |
|---|---|
| `data/health.db` | Alle Rohdaten (Garmin-Antworten je Tag: Schlaf, HRV, Puls, Stress, Body Battery, Atmung, SpO2, Schritte, Intensitätsminuten, Trainingsstatus, VO2max, Trinken), eine Zeile Kennzahlen pro Tag und alle Aktivitäten (Tabelle `activities`) |
| `data/activities/<id>.zip` | Original-FIT-Datei jeder Aktivität |
| `data/out/health_summary.json` | Letzte 14 Tage, 7- und 28-Tage-Schnitte, Warnsignale, Stundenpuls der letzten 3 Tage |
| `data/out/health_daily.csv` | Eine Zeile pro Tag, letzte 90 Tage |

Beide Dateien in `data/out/` werden nach jedem Sync nach `RCLONE_REMOTE` kopiert.

**Wichtig:** Das Repo ist öffentlich. Gesundheitsdaten, `.env` und `data/`
gehören nie ins Repo (`.gitignore` schließt sie aus).

## Einrichtung auf der NAS

1. Ordner `healthsync/` auf die NAS kopieren, z. B. nach `/volume1/docker/healthsync`.
2. `cp .env.example .env` und Garmin-E-Mail und -Passwort eintragen.
3. Google Drive für rclone freigeben (einmalig):
   - Auf dem Mac: `brew install rclone` und `rclone authorize "drive"`.
     Im Browser anmelden, das ausgegebene Token kopieren.
   - Auf der NAS: `docker compose run --rm --entrypoint rclone healthsync config`
     → neues Remote `gdrive`, Typ `drive`, Scope `drive.file`, bei
     „Use web browser?“ **n** wählen und das Token vom Mac einfügen.
     Die Konfiguration landet in `data/rclone.conf`.
4. Garmin-Login mit Zwei-Faktor-Code (einmalig, Tokens halten ca. ein Jahr):
   `docker compose run --rm healthsync login`
5. Testlauf: `docker compose run --rm healthsync sync --days 30`
6. Dauerbetrieb: `docker compose up -d --build`
7. Einmalig die ganze Historie holen (läuft Stunden, darf abbrechen und neu starten,
   gespeicherte Tage werden übersprungen):
   `docker compose run -d --name healthsync-backfill healthsync backfill --since 2019-01-01`

Danach synchronisiert der Container zu den Zeiten in `SYNC_TIMES`
(Standard 07:30, 13:00 und 21:30) und holt jeweils die letzten `SYNC_DAYS` Tage
neu, damit nachträglich synchronisierte Uhrdaten auch ankommen.

## Apple-Health-Verlauf importieren

Für die komplette Vergangenheit von Waage (Zepp Life), Yazio und allem anderen
in Apple Health:

1. iPhone → Health-App → Profilbild → „Alle Gesundheitsdaten exportieren“.
2. Die `export.zip` nach `data/inbox/` auf der NAS kopieren (z. B. per AirDrop
   an den Mac, dann `scp export.zip nashorn:/volume1/docker/healthsync/data/inbox/`).
3. `docker compose run --rm healthsync import-apple`

Jeder Messwert landet in der Tabelle `apple_records`, Gewicht, Körperfett und
Ernährung zusätzlich als Tageswerte (Quelle `apple`) in `daily`. Mehrfach
importieren ist unschädlich, doppelte Werte werden übersprungen. Bei gleichen
Kennzahlen gilt die Reihenfolge Garmin, Health Auto Export, Apple-Export.

## Gewicht und Ernährung (Health Auto Export)

Die Waage (Zepp Life) und Yazio schreiben in Apple Health. Die iPhone-App
**Health Auto Export** schickt diese Werte an den Container:

1. In `.env` `HAE_PORT=8085` und ein langes Zufallswort als `HAE_TOKEN` setzen,
   dann `docker compose up -d`.
2. In der App: *Automations* → neue Automation → *REST API*.
   - URL: `http://<IP-der-NAS>:8085/`
   - Header: `X-Token` = dein `HAE_TOKEN`
   - Format JSON, Daten: *Health Metrics*, Zeitraum „Default“/letzte 7 Tage,
     Aggregation täglich.
   - Metriken: Gewicht, Körperfett, fettfreie Masse, BMI, Nahrungsenergie,
     Protein, Kohlenhydrate, Fett, Ballaststoffe, Wasser.
   - Intervall z. B. stündlich.
3. Einmal „Manuell ausführen“ und in `docker logs healthsync` nach
   „Health Auto Export: … days received“ schauen.

Das iPhone erreicht die NAS nur im Heimnetz (oder per VPN). Den Port bitte
nicht ins Internet freigeben.

## Trainingsplan auf die Uhr

Der Coach schreibt den Plan als `plan.json` in den Drive-Ordner
`Workout-Coach`. Der Container schaut alle `PLAN_CHECK_MINUTES` Minuten
(Standard 15) nach Änderungen und legt jede Einheit als Workout im
Garmin-Kalender an: Läufe mit Pace- oder Pulszielen, Intervalle als
Wiederholungen, Krafteinheiten mit Übung, Sätzen, Wiederholungen, Gewicht und
Pausen. Nach dem nächsten Sync der Uhr stehen sie dort unter „Training“ bzw. im
Kalender.

- Geänderte Einheiten werden ersetzt, gestrichene (zukünftige) gelöscht.
- Workouts, die du selbst in Garmin Connect angelegt hast, bleiben unberührt.
- Gibt es noch keine `plan.json`, legt der Container eine leere an. Sie muss
  von rclone stammen, weil rclone nur eigene Dateien sehen darf.
- Von Hand: `docker compose run --rm healthsync push-plan` (oder
  `--file plan.json`). Das Format steht oben in `healthsync/workouts.py`.

## Wenn etwas hakt

- `docker logs healthsync` zeigt jeden Lauf.
- Garmin meldet „Authentication failed“: Schritt 4 wiederholen.
- Die Garmin-Anbindung nutzt die inoffizielle Bibliothek
  [`garminconnect`](https://github.com/cyberjunky/python-garminconnect).
  Wenn Garmin den Login ändert, hilft meist ein Update der Version in
  `requirements.txt` und `docker compose up -d --build`.

## Tests

```
pip install garminconnect pytest
python -m pytest tests
```
