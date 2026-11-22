# healthsync

Holt jeden Tag Schlaf, HRV, Ruhepuls, Puls über den Tag, Stress, Body Battery,
Training Readiness und (falls vorhanden) Gewicht aus Garmin Connect, speichert
alles in einer SQLite-Datenbank auf der NAS und legt eine Tageszusammenfassung
in Google Drive ab. Von dort liest der Coach (Claude) die Daten.

Läuft als ein Docker-Container und berührt keine anderen Container.

## Was entsteht

| Datei | Inhalt |
|---|---|
| `data/health.db` | Alle Rohdaten (Garmin-Antworten) und eine Zeile Kennzahlen pro Tag |
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

Danach synchronisiert der Container zu den Zeiten in `SYNC_TIMES`
(Standard 07:30, 13:00 und 21:30) und holt jeweils die letzten `SYNC_DAYS` Tage
neu, damit nachträglich synchronisierte Uhrdaten auch ankommen.

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
