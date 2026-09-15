#!/usr/bin/env python3
"""Small, dependency-free ANPR dashboard and ingest API."""
import json
import os
import sqlite3
import base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("ANPR_DB", ROOT / "anpr.sqlite3"))
PORT = int(os.environ.get("PORT", "8022"))


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, plate TEXT NOT NULL,
        confidence REAL, camera TEXT, direction TEXT, vehicle_type TEXT,
        captured_at TEXT NOT NULL, image_url TEXT, raw_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS cameras (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        location TEXT, endpoint TEXT, enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    for name, definition in (("vehicle_color", "TEXT"), ("thumbnail", "BLOB"), ("vehicle_image", "BLOB")):
        if name not in columns:
            conn.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")
    return conn


def normalise(payload):
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    picture = payload.get("Picture") if isinstance(payload.get("Picture"), dict) else {}
    plate_info = picture.get("Plate") if isinstance(picture.get("Plate"), dict) else {}
    snap_info = picture.get("SnapInfo") if isinstance(picture.get("SnapInfo"), dict) else {}
    vehicle_info = picture.get("Vehicle") if isinstance(picture.get("Vehicle"), dict) else {}
    thumbnail = image_bytes(picture.get("CutoutPic"), "Picture.CutoutPic", 256 * 1024)
    vehicle_image = image_bytes(picture.get("VehiclePic"), "Picture.VehiclePic", 2 * 1024 * 1024)
    plate = plate_info.get("PlateNumber")
    if not plate or not isinstance(plate, str):
        raise ValueError("plate is required")
    confidence = plate_info.get("Confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
            if not 0 <= confidence <= 100: raise ValueError
            confidence /= 100
        except (TypeError, ValueError):
            raise ValueError("Picture.Plate.Confidence must be between 0 and 100")
    captured = snap_info.get("AccurateTime")
    if captured:
        try: datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
        except ValueError: raise ValueError("captured_at must be ISO-8601")
    else: captured = datetime.now(timezone.utc).isoformat()
    return {
        "plate": plate.strip().upper(), "confidence": confidence,
        "camera": "Camera feed",
        "direction": str(snap_info.get("Direction") or "Unknown"),
        "vehicle_type": "Unknown",
        "vehicle_color": str(vehicle_info.get("VehicleColor") or "Unknown"),
        "thumbnail": thumbnail,
        "vehicle_image": vehicle_image,
        "captured_at": captured, "image_url": None
    }


def image_bytes(image, field, maximum_size):
    if not isinstance(image, dict) or not image.get("Content"):
        return None
    try:
        data = base64.b64decode(image["Content"], validate=True)
    except (ValueError, TypeError):
        raise ValueError(f"{field}.Content must be valid Base64")
    if len(data) > maximum_size:
        raise ValueError(f"{field} exceeds the {maximum_size // 1024} KiB limit")
    if not data.startswith(b"\xff\xd8\xff"):
        raise ValueError(f"{field} must be a JPEG image")
    return data


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if content_type == "application/json" else body.encode())
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers(); self.wfile.write(data)

    def do_OPTIONS(self): self._send(204, b"")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health": return self._send(200, {"status": "ok"})
        if parsed.path == "/api/cameras":
            conn = db(); rows = conn.execute("SELECT * FROM cameras ORDER BY name").fetchall(); conn.close()
            return self._send(200, {"cameras": [dict(r) for r in rows]})
        if parsed.path == "/api/events":
            q = parse_qs(parsed.query); limit = min(int(q.get("limit", [50])[0]), 500)
            conn = db(); rows = conn.execute("SELECT id,plate,confidence,camera,direction,vehicle_type,vehicle_color,captured_at,image_url,created_at,thumbnail IS NOT NULL AS has_thumbnail,vehicle_image IS NOT NULL AS has_vehicle_image FROM events ORDER BY captured_at DESC LIMIT ?", (limit,)).fetchall(); conn.close()
            return self._send(200, {"events": [dict(r) for r in rows]})
        if parsed.path.startswith("/api/events/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 4 and parts[3] in ("thumbnail", "vehicle"):
                try:
                    event_id = int(parts[2]); column = "thumbnail" if parts[3] == "thumbnail" else "vehicle_image"
                    conn = db(); row = conn.execute(f"SELECT {column} FROM events WHERE id=?", (event_id,)).fetchone(); conn.close()
                    if not row or not row[column]: return self._send(404, {"error": "Image not found"})
                    return self._send(200, row[column], "image/jpeg")
                except ValueError: return self._send(400, {"error": "Invalid event id"})
        if parsed.path == "/api/analytics":
            conn = db(); total = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
            today = datetime.now(timezone.utc).date().isoformat(); today_count = conn.execute("SELECT COUNT(*) c FROM events WHERE substr(captured_at,1,10)=?", (today,)).fetchone()["c"]
            unique = conn.execute("SELECT COUNT(DISTINCT plate) c FROM events").fetchone()["c"]; avg = conn.execute("SELECT AVG(confidence) v FROM events WHERE confidence IS NOT NULL").fetchone()["v"]
            hours = conn.execute("SELECT substr(captured_at,12,2) hour, COUNT(*) count FROM events GROUP BY hour ORDER BY hour").fetchall(); top = conn.execute("SELECT plate, COUNT(*) count FROM events GROUP BY plate ORDER BY count DESC LIMIT 5").fetchall(); conn.close()
            return self._send(200, {"total": total, "today": today_count, "unique_plates": unique, "avg_confidence": avg, "hourly": [dict(r) for r in hours], "top_plates": [dict(r) for r in top]})
        if parsed.path in ("/", "/index.html"):
            return self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if parsed.path == "/app.js": return self._send(200, (ROOT / "app.js").read_bytes(), "text/javascript")
        if parsed.path == "/styles.css": return self._send(200, (ROOT / "styles.css").read_bytes() + (ROOT / "camera.css").read_bytes() + (ROOT / "image.css").read_bytes(), "text/css")
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/cameras":
            try:
                length = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(length))
                name = str(payload.get("name", "")).strip(); location = str(payload.get("location", "")).strip(); endpoint = str(payload.get("endpoint", "")).strip()
                if not name: raise ValueError("name is required")
                if len(name) > 80: raise ValueError("name is too long")
                now = datetime.now(timezone.utc).isoformat(); conn = db(); cur = conn.execute("INSERT INTO cameras (name,location,endpoint,enabled,created_at) VALUES (?,?,?,?,?)", (name, location, endpoint, 1, now)); conn.commit(); row = conn.execute("SELECT * FROM cameras WHERE id=?", (cur.lastrowid,)).fetchone(); conn.close(); return self._send(201, {"camera": dict(row)})
            except (ValueError, json.JSONDecodeError) as e: return self._send(400, {"error": str(e)})
        if path != "/api/events": return self._send(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(length)); event = normalise(payload)
            stored_payload = {key: event[key] for key in ("plate", "confidence", "direction", "vehicle_color", "captured_at")}
            conn = db(); now = datetime.now(timezone.utc).isoformat(); cur = conn.execute("INSERT INTO events (plate,confidence,camera,direction,vehicle_type,vehicle_color,thumbnail,vehicle_image,captured_at,image_url,raw_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (*event.values(), json.dumps(stored_payload), now)); conn.commit(); event["id"] = cur.lastrowid; event["has_thumbnail"] = bool(event.pop("thumbnail")); event["has_vehicle_image"] = bool(event.pop("vehicle_image")); conn.close(); self._send(201, {"event": event})
        except (ValueError, json.JSONDecodeError) as e: self._send(400, {"error": str(e)})
        except Exception as e: self._send(500, {"error": "Internal server error", "detail": str(e)})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/cameras/"): return self._send(404, {"error": "Not found"})
        try:
            camera_id = int(path.rsplit("/", 1)[1]); conn = db(); cur = conn.execute("DELETE FROM cameras WHERE id=?", (camera_id,)); conn.commit(); conn.close()
            if not cur.rowcount: return self._send(404, {"error": "Camera not found"})
            self._send(200, {"deleted": camera_id})
        except ValueError: self._send(400, {"error": "Invalid camera id"})

    def log_message(self, *_): pass


if __name__ == "__main__":
    db().close(); print(f"ANPR dashboard running at http://localhost:{PORT}"); ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
