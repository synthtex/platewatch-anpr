# Platewatch ANPR dashboard

Run with Python 3.9+: `python3 app.py`, then open http://localhost:8022.

Send detections to `POST /api/events` as JSON:

```json
{"plate":"ABC123","confidence":0.96,"camera":"Gate A","direction":"in","vehicle_type":"car","captured_at":"2026-09-14T10:30:00Z"}
```

The SQLite database is created as `anpr.sqlite3` (override with `ANPR_DB`). Camera events use the nested `Picture.Plate`, `Picture.SnapInfo`, and `Picture.Vehicle` payload fields.

Use `GET /api/events?plate=ABC&limit=100` to search all stored events by a partial plate number and choose up to 500 returned events.

## Docker

Run `docker compose up -d --build`, then open `http://localhost:8022`. SQLite is persisted in the `platewatch-data` volume.

## Home Assistant add-on

The `addon/` directory contains the add-on metadata and container definition. Add it to a Home Assistant add-on repository, install it from the add-on store, and enable Ingress to open Platewatch inside Home Assistant.
