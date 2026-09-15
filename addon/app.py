#!/usr/bin/env python3
"""Small, dependency-free ANPR dashboard and ingest API."""
import json
import os
import sqlite3
import base64
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from paho.mqtt import publish as mqtt_publish
except ImportError:
    mqtt_publish = None

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("ANPR_DB", ROOT / "anpr.sqlite3"))
PORT = int(os.environ.get("PORT", "8022"))
MQTT_DEFAULTS = {
    "mqtt_enabled": "false", "mqtt_host": "core-mosquitto", "mqtt_port": "1883",
    "mqtt_username": "", "mqtt_password": "", "mqtt_client_id": "platewatch-anpr",
    "mqtt_topic": "platewatch/last_detection", "mqtt_discovery_prefix": "homeassistant",
}


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
    conn.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")
    conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('retention_days','30')")
    for key, value in MQTT_DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (key, value))
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    for name, definition in (("vehicle_color", "TEXT"), ("thumbnail", "BLOB"), ("vehicle_image", "BLOB")):
        if name not in columns:
            conn.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")
    conn.commit()
    return conn


def retention_days(conn):
    value = conn.execute("SELECT value FROM settings WHERE key='retention_days'").fetchone()["value"]
    return int(value)


def mqtt_settings(conn):
    rows = conn.execute("SELECT key,value FROM settings WHERE key LIKE 'mqtt_%'").fetchall()
    return {key: {"true": True, "false": False}.get(value, value) for key, value in {**MQTT_DEFAULTS, **{row["key"]: row["value"] for row in rows}}.items()}


def public_settings(conn):
    mqtt = mqtt_settings(conn)
    password_configured = bool(mqtt.pop("mqtt_password"))
    return {"retention_days": retention_days(conn), "mqtt": mqtt, "mqtt_password_configured": password_configured}


def mqtt_connection(settings):
    if mqtt_publish is None:
        raise RuntimeError("MQTT support is unavailable in this installation")
    if not settings["mqtt_enabled"]:
        raise ValueError("Enable MQTT before testing the connection")
    auth = {"username": settings["mqtt_username"], "password": settings["mqtt_password"]} if settings["mqtt_username"] else None
    return {"hostname": settings["mqtt_host"], "port": int(settings["mqtt_port"]), "auth": auth, "client_id": settings["mqtt_client_id"], "keepalive": 10}


def publish_detection(settings, event):
    try:
        connection = mqtt_connection(settings)
        topic = settings["mqtt_topic"].strip("/")
        discovery_topic = f"{settings['mqtt_discovery_prefix'].strip('/')}/sensor/platewatch_last_detection/config"
        discovery = {"name": "Platewatch Last Detection", "unique_id": "platewatch_last_detection", "state_topic": topic, "value_template": "{{ value_json.plate }}", "json_attributes_topic": topic, "icon": "mdi:car-search", "device": {"identifiers": ["platewatch_anpr"], "name": "Platewatch ANPR", "manufacturer": "Platewatch"}}
        mqtt_publish.single(discovery_topic, json.dumps(discovery), retain=True, **connection)
        message = {"plate": event["plate"], "direction": event["direction"], "time": event["captured_at"]}
        mqtt_publish.single(topic, json.dumps(message), retain=True, **connection)
    except Exception as error:
        print(f"MQTT publish failed: {error}")


def purge_expired_events(conn):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days(conn))).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM events WHERE datetime(captured_at) < datetime(?)", (cutoff,))
    conn.commit()


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
        if parsed.path == "/api/settings":
            conn = db(); settings = public_settings(conn); conn.close()
            return self._send(200, settings)
        if parsed.path == "/api/cameras":
            conn = db(); rows = conn.execute("SELECT * FROM cameras ORDER BY name").fetchall(); conn.close()
            return self._send(200, {"cameras": [dict(r) for r in rows]})
        if parsed.path == "/api/events":
            q = parse_qs(parsed.query)
            try: limit = min(max(int(q.get("limit", [50])[0]), 1), 500)
            except ValueError: return self._send(400, {"error": "limit must be between 1 and 500"})
            plate_query = q.get("plate", [""])[0].strip().upper()
            if len(plate_query) > 64: return self._send(400, {"error": "plate search is too long"})
            sql = "SELECT id,plate,confidence,camera,direction,vehicle_type,vehicle_color,captured_at,image_url,created_at,thumbnail IS NOT NULL AS has_thumbnail,vehicle_image IS NOT NULL AS has_vehicle_image FROM events"
            parameters = []
            if plate_query:
                sql += " WHERE plate LIKE ?"; parameters.append(f"%{plate_query}%")
            sql += " ORDER BY captured_at DESC LIMIT ?"; parameters.append(limit)
            conn = db(); purge_expired_events(conn); rows = conn.execute(sql, parameters).fetchall(); conn.close()
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
            conn = db(); purge_expired_events(conn); total = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
            today = datetime.now(timezone.utc).date().isoformat(); today_count = conn.execute("SELECT COUNT(*) c FROM events WHERE substr(captured_at,1,10)=?", (today,)).fetchone()["c"]
            unique = conn.execute("SELECT COUNT(DISTINCT plate) c FROM events").fetchone()["c"]; avg = conn.execute("SELECT AVG(confidence) v FROM events WHERE confidence IS NOT NULL").fetchone()["v"]
            hours = conn.execute("SELECT substr(captured_at,12,2) hour, COUNT(*) count FROM events GROUP BY hour ORDER BY hour").fetchall(); top = conn.execute("SELECT plate, COUNT(*) count FROM events GROUP BY plate ORDER BY count DESC LIMIT 5").fetchall(); conn.close()
            return self._send(200, {"total": total, "today": today_count, "unique_plates": unique, "avg_confidence": avg, "hourly": [dict(r) for r in hours], "top_plates": [dict(r) for r in top]})
        if parsed.path in ("/", "/index.html"):
            return self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if parsed.path == "/app.js": return self._send(200, (ROOT / "app.js").read_bytes(), "text/javascript")
        if parsed.path == "/styles.css": return self._send(200, (ROOT / "styles.css").read_bytes() + (ROOT / "camera.css").read_bytes() + (ROOT / "image.css").read_bytes(), "text/css")
        self._send(404, {"error": "Not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/cameras/"): return self._send(404, {"error": "Not found"})
        try:
            camera_id = int(path.rsplit("/", 1)[1]); conn = db(); cur = conn.execute("DELETE FROM cameras WHERE id=?", (camera_id,)); conn.commit(); conn.close()
            if not cur.rowcount: return self._send(404, {"error": "Camera not found"})
            self._send(200, {"deleted": camera_id})
        except ValueError: self._send(400, {"error": "Invalid camera id"})

    def do_PUT(self):
        if urlparse(self.path).path != "/api/settings": return self._send(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(length)); conn = db()
            if "retention_days" in payload:
                days = int(payload["retention_days"])
                if not 1 <= days <= 3650: raise ValueError("retention_days must be between 1 and 3650")
                conn.execute("UPDATE settings SET value=? WHERE key='retention_days'", (str(days),)); purge_expired_events(conn)
            if "mqtt" in payload:
                mqtt = payload["mqtt"]
                if not isinstance(mqtt, dict): raise ValueError("mqtt must be an object")
                host = str(mqtt.get("mqtt_host", "")).strip(); port = int(mqtt.get("mqtt_port", 1883))
                if not host: raise ValueError("MQTT host is required")
                if not 1 <= port <= 65535: raise ValueError("MQTT port must be between 1 and 65535")
                for key, label in (("mqtt_client_id", "MQTT client ID"), ("mqtt_topic", "MQTT state topic"), ("mqtt_discovery_prefix", "MQTT discovery prefix")):
                    if not str(mqtt.get(key, "")).strip(): raise ValueError(f"{label} is required")
                for key in ("mqtt_enabled", "mqtt_host", "mqtt_port", "mqtt_username", "mqtt_client_id", "mqtt_topic", "mqtt_discovery_prefix"):
                    value = mqtt.get(key, MQTT_DEFAULTS[key]); conn.execute("UPDATE settings SET value=? WHERE key=?", (str(value).lower() if key == "mqtt_enabled" else str(value).strip(), key))
                if mqtt.get("mqtt_password"):
                    conn.execute("UPDATE settings SET value=? WHERE key='mqtt_password'", (str(mqtt["mqtt_password"]),))
            conn.commit(); response = public_settings(conn); conn.close(); self._send(200, response)
        except (ValueError, TypeError, json.JSONDecodeError) as e: self._send(400, {"error": str(e)})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/mqtt/test":
            try:
                conn = db(); settings = mqtt_settings(conn); conn.close(); connection = mqtt_connection(settings)
                topic = f"{settings['mqtt_discovery_prefix'].strip('/')}/sensor/platewatch_last_detection/config"
                mqtt_publish.single(topic, json.dumps({"name": "Platewatch Last Detection", "unique_id": "platewatch_last_detection", "state_topic": settings["mqtt_topic"].strip("/"), "value_template": "{{ value_json.plate }}", "json_attributes_topic": settings["mqtt_topic"].strip("/"), "icon": "mdi:car-search"}), retain=True, **connection)
                return self._send(200, {"status": "connected"})
            except (ValueError, RuntimeError) as e: return self._send(400, {"error": str(e)})
            except Exception as e: return self._send(502, {"error": f"MQTT connection failed: {e}"})
        if path == "/api/cameras":
            return self._create_camera()
        if path != "/api/events": return self._send(404, {"error": "Not found"})
        return self._create_event()

    def _create_camera(self):
        try:
            length = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(length))
            name = str(payload.get("name", "")).strip(); location = str(payload.get("location", "")).strip(); endpoint = str(payload.get("endpoint", "")).strip()
            if not name: raise ValueError("name is required")
            if len(name) > 80: raise ValueError("name is too long")
            now = datetime.now(timezone.utc).isoformat(); conn = db(); cur = conn.execute("INSERT INTO cameras (name,location,endpoint,enabled,created_at) VALUES (?,?,?,?,?)", (name, location, endpoint, 1, now)); conn.commit(); row = conn.execute("SELECT * FROM cameras WHERE id=?", (cur.lastrowid,)).fetchone(); conn.close(); self._send(201, {"camera": dict(row)})
        except (ValueError, json.JSONDecodeError) as e: self._send(400, {"error": str(e)})

    def _create_event(self):
        try:
            length = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(length)); event = normalise(payload)
            stored_payload = {key: event[key] for key in ("plate", "confidence", "direction", "vehicle_color", "captured_at")}
            conn = db(); purge_expired_events(conn); now = datetime.now(timezone.utc).isoformat(); cur = conn.execute("INSERT INTO events (plate,confidence,camera,direction,vehicle_type,vehicle_color,thumbnail,vehicle_image,captured_at,image_url,raw_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (*event.values(), json.dumps(stored_payload), now)); conn.commit(); settings = mqtt_settings(conn); event["id"] = cur.lastrowid; event["has_thumbnail"] = bool(event.pop("thumbnail")); event["has_vehicle_image"] = bool(event.pop("vehicle_image")); conn.close(); threading.Thread(target=publish_detection, args=(settings, event), daemon=True).start(); self._send(201, {"event": event})
        except (ValueError, json.JSONDecodeError) as e: self._send(400, {"error": str(e)})
        except Exception as e: self._send(500, {"error": "Internal server error", "detail": str(e)})

    def log_message(self, *_): pass


if __name__ == "__main__":
    db().close(); print(f"ANPR dashboard running at http://localhost:{PORT}"); ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
