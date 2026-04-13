"""Project Hawk v5 – Production Flask Application"""
import os, cv2, time, uuid, threading, smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from flask import Flask, Response, render_template, jsonify, request, send_from_directory
from modules.video_engine import VideoEngine
from modules.alert import AlertManager
from modules.zone_manager import ZoneManager, Zone, ZoneRule

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)

alert_manager = AlertManager(save_dir="detected_frames", cooldown_sec=8.0)
zone_manager  = ZoneManager()
cameras: dict = {}
_notif_log: list = []
_settings: dict = {
    "confidence_threshold": 0.45,
    "alert_cooldown_sec": 8,
    "skip_frames": 2,
    "jpeg_quality": 78,
    "max_snapshots": 50,
    "demo_mode": False,
    "email_alerts": False,
    "email_recipient": "",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_pass": "",
}
_system_start = time.time()


# ── Notification helpers ──────────────────────────────────────────

def _push_notif(title, body, level="info", cam=None, zone=None):
    _notif_log.append({
        "id": str(uuid.uuid4())[:8],
        "title": title, "body": body, "level": level,
        "cam": cam, "zone": zone,
        "ts": datetime.now().isoformat(), "read": False,
    })
    if len(_notif_log) > 300:
        _notif_log[:] = _notif_log[-300:]


# ── Email helpers ─────────────────────────────────────────────────

def _do_send_email(subject, body_html, snapshot_path=None):
    """Core SMTP sending — returns (True, '') or (False, error_message)."""
    try:
        recipient = _settings.get("email_recipient", "").strip()
        smtp_host = _settings.get("smtp_host", "smtp.gmail.com").strip()
        smtp_port = int(_settings.get("smtp_port", 587))
        smtp_user = _settings.get("smtp_user", "").strip()
        smtp_pass = _settings.get("smtp_pass", "").strip()

        if not recipient:
            return False, "Recipient email is empty"
        if not smtp_user:
            return False, "Username (Gmail ID) is empty"
        if not smtp_pass:
            return False, "App Password is empty"

        msg = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"]    = f"Hawk Surveillance <{smtp_user}>"
        msg["To"]      = recipient
        alt = MIMEMultipart("alternative")
        msg.attach(alt)
        alt.attach(MIMEText(body_html, "html"))

        if snapshot_path and os.path.exists(snapshot_path):
            with open(snapshot_path, "rb") as f:
                img = MIMEImage(f.read(), name=os.path.basename(snapshot_path))
                img.add_header("Content-ID", "<snapshot>")
                img.add_header("Content-Disposition", "inline")
                msg.attach(img)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, recipient, msg.as_string())

        print(f"[HAWK Email] ✓ Sent: {subject} → {recipient}")
        return True, ""

    except smtplib.SMTPAuthenticationError:
        return False, "Authentication failed — check your Gmail ID and App Password"
    except smtplib.SMTPConnectError:
        return False, "Cannot connect to SMTP server — check host and port"
    except smtplib.SMTPRecipientsRefused:
        return False, "Recipient email was refused by the server"
    except Exception as e:
        return False, str(e)


def _send_email(subject, body_html, snapshot_path=None):
    """Send email in background thread (fire-and-forget for alerts)."""
    def _worker():
        ok, err = _do_send_email(subject, body_html, snapshot_path)
        if not ok:
            print(f"[HAWK Email] ✗ {err}")
    threading.Thread(target=_worker, daemon=True).start()


def _alert_email(alert_type, zone, msg, snapshot_path=None):
    if not _settings.get("email_alerts"):
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    snap_tag = '<img src="cid:snapshot" style="max-width:100%;border-radius:8px;margin-top:16px;" alt="Snapshot"/>' if snapshot_path else ""
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:600px;margin:auto;background:#0d1117;color:#e8edf4;border-radius:12px;overflow:hidden;border:1px solid #1f2d3d;">
      <div style="background:linear-gradient(135deg,#0088cc,#00c9ff);padding:20px 24px;">
        <h1 style="margin:0;font-size:20px;color:#fff;font-weight:700;">🦅 Hawk Surveillance Alert</h1>
      </div>
      <div style="padding:24px;">
        <div style="background:#1a2332;border-radius:8px;border-left:4px solid #ff4757;padding:14px 16px;margin-bottom:20px;">
          <div style="font-size:11px;color:#8b9ab0;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px;">Alert Type</div>
          <div style="font-size:17px;font-weight:700;color:#fff;">{alert_type}</div>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <tr><td style="padding:8px 0;color:#8b9ab0;width:100px;vertical-align:top;">Zone</td><td style="color:#e8edf4;font-weight:500;">{zone or "—"}</td></tr>
          <tr><td style="padding:8px 0;color:#8b9ab0;vertical-align:top;">Message</td><td style="color:#e8edf4;">{msg}</td></tr>
          <tr><td style="padding:8px 0;color:#8b9ab0;vertical-align:top;">Timestamp</td><td style="color:#e8edf4;font-family:monospace;">{ts}</td></tr>
        </table>
        {snap_tag}
        <div style="margin-top:24px;padding-top:16px;border-top:1px solid #1f2d3d;font-size:11px;color:#4a5668;">
          Automated alert from Project Hawk Surveillance System v5.0
        </div>
      </div>
    </div>"""
    _send_email(f"[HAWK ALERT] {alert_type} — {zone or 'Camera'}", html, snapshot_path)


# ── Camera helpers ────────────────────────────────────────────────

def _make_engine(cam_id, source, name=None):
    engine = VideoEngine(
        source=source, camera_id=cam_id,
        skip_frames=_settings["skip_frames"],
        alert_manager=alert_manager, zone_manager=zone_manager
    )
    engine.meta = {"name": name or cam_id, "source": str(source), "added": datetime.now().isoformat()}
    engine._email_callback = _alert_email   # wire zone alert emails
    cameras[cam_id] = engine
    engine.start()
    _push_notif("Camera Connected", f"{name or cam_id} is now live", "info", cam=cam_id)
    return engine

def _get_or_create(cam_id, source, name=None):
    if cam_id not in cameras:
        _make_engine(cam_id, source, name)
    return cameras[cam_id]

try:
    _get_or_create("CAM-01", 0, "Main Camera")
except Exception as e:
    print(f"[HAWK] Warning: default camera unavailable — {e}")


# ── Placeholder frame ─────────────────────────────────────────────

def _make_placeholder(label="NO SIGNAL"):
    import numpy as np
    img = np.full((360, 640, 3), 18, dtype="uint8")
    cv2.putText(img, label, (220, 168), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (50, 65, 80), 2, cv2.LINE_AA)
    cv2.putText(img, "Camera offline or unavailable", (148, 208), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (35, 50, 60), 1, cv2.LINE_AA)
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 55])
    return buf.tobytes()

_placeholder_bytes = _make_placeholder()


# ── MJPEG ────────────────────────────────────────────────────────

def _mjpeg(engine):
    q = _settings["jpeg_quality"]
    while True:
        frame = engine.get_frame()
        if frame is None:
            payload = _placeholder_bytes
        else:
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, q])
            payload = buf.tobytes() if ok else _placeholder_bytes
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + payload + b"\r\n"
        time.sleep(1 / 25)


# ── Page routes ──────────────────────────────────────────────────

@app.route("/")
@app.route("/dashboard")
def dashboard(): return render_template("dashboard.html")

@app.route("/zones")
def zones_page(): return render_template("zones.html")

@app.route("/alerts")
def alerts_page(): return render_template("alerts.html")

@app.route("/analytics")
def analytics_page(): return render_template("analytics.html")

@app.route("/settings")
def settings_page(): return render_template("settings.html")

@app.route("/snapshots")
def snapshots_page(): return render_template("snapshots.html")

@app.route("/onboarding")
def onboarding(): return render_template("dashboard.html")


# ── Video feed ───────────────────────────────────────────────────

@app.route("/video_feed/<cam_id>")
def video_feed(cam_id):
    e = cameras.get(cam_id)
    if not e:
        def _dead():
            while True:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + _placeholder_bytes + b"\r\n"
                time.sleep(2)
        return Response(_dead(), mimetype="multipart/x-mixed-replace; boundary=frame")
    return Response(_mjpeg(e), mimetype="multipart/x-mixed-replace; boundary=frame")


# ── Camera API ──────────────────────────────────────────────────

@app.route("/api/cameras", methods=["GET"])
def list_cameras():
    out = {}
    for cid, e in cameras.items():
        s = e.get_stats()
        out[cid] = {
            "id": cid,
            "name": getattr(e, "meta", {}).get("name", cid),
            "source": getattr(e, "meta", {}).get("source", ""),
            "added": getattr(e, "meta", {}).get("added", ""),
            "fps": s.get("fps", 0),
            "recording": s.get("recording", False),
            "detections_total": s.get("detections_total", 0),
            "alerts_total": s.get("alerts_total", 0),
            "online": e.get_frame() is not None,
        }
    return jsonify(out)

@app.route("/api/camera/start", methods=["POST"])
def cam_start():
    d = request.get_json(force=True)
    src = d.get("source", 0)
    try: src = int(src)
    except (ValueError, TypeError): pass
    cam_id = d.get("cam_id") or f"CAM-{str(uuid.uuid4())[:4].upper()}"
    try:
        _get_or_create(cam_id, src, d.get("name", cam_id))
        return jsonify({"status": "started", "cam_id": cam_id, "cameras": list(cameras.keys())})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/camera/stop", methods=["POST"])
def cam_stop():
    cid = request.get_json(force=True).get("cam_id")
    e = cameras.pop(cid, None)
    if e:
        e.stop()
        _push_notif("Camera Removed", f"{cid} disconnected", "info", cam=cid)
        return jsonify({"status": "stopped", "cameras": list(cameras.keys())})
    return jsonify({"status": "not_found"}), 404

@app.route("/api/camera/<cam_id>/rename", methods=["POST"])
def cam_rename(cam_id):
    e = cameras.get(cam_id)
    if not e: return jsonify({"status": "not_found"}), 404
    name = request.get_json(force=True).get("name", cam_id)
    if not hasattr(e, "meta"): e.meta = {}
    e.meta["name"] = name
    return jsonify({"status": "ok"})

@app.route("/api/camera/<cam_id>/record", methods=["POST"])
def cam_record(cam_id):
    e = cameras.get(cam_id)
    if not e: return jsonify({"status": "not_found"}), 404
    e.start_recording()
    return jsonify({"status": "recording"})

@app.route("/api/heatmap", methods=["POST"])
def heatmap():
    v = request.get_json(force=True).get("enabled", False)
    for e in cameras.values(): e.set_heatmap(v)
    return jsonify({"heatmap": v})


# ── Stats / Summary ─────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    out = {}
    for cid, e in cameras.items():
        s = e.get_stats()
        s["zone_stats"] = zone_manager.get_stats()
        s["fps_history"] = e._fps_history[-30:]
        s["online"] = e.get_frame() is not None
        out[cid] = s
    return jsonify(out)

@app.route("/api/summary")
def api_summary():
    uptime = int(time.time() - _system_start)
    total_dets = sum(e.stats["detections_total"] for e in cameras.values())
    total_alts = sum(e.stats["alerts_total"] for e in cameras.values())
    total_zalt = sum(e.stats["zone_alerts"] for e in cameras.values())
    zs = zone_manager.get_stats()
    unread = sum(1 for n in _notif_log if not n["read"])
    return jsonify({
        "cameras": len(cameras),
        "cameras_online": sum(1 for e in cameras.values() if e.get_frame() is not None),
        "detections": total_dets, "alerts": total_alts, "zone_alerts": total_zalt,
        "zones": zs, "uptime_sec": uptime,
        "unread_notifications": unread,
        "demo_mode": _settings["demo_mode"],
    })

@app.route("/api/detections")
def api_detections():
    limit = int(request.args.get("limit", 50))
    return jsonify({c: e.get_recent_detections(limit) for c, e in cameras.items()})

@app.route("/api/zone_alerts")
def api_zone_alerts():
    alerts = []
    for e in cameras.values(): alerts.extend(e.get_zone_alerts(60))
    alerts.sort(key=lambda x: x.get("ts", x.get("timestamp", "")), reverse=True)
    return jsonify({"alerts": alerts[:60]})

@app.route("/api/events")
def api_events():
    limit = int(request.args.get("limit", 100))
    sev   = request.args.get("severity")
    zone  = request.args.get("zone")
    events = zone_manager.get_event_log(limit * 3)
    if sev:  events = [e for e in events if e.get("severity") == sev]
    if zone: events = [e for e in events if e.get("zone_name") == zone]
    return jsonify(events[-limit:])

@app.route("/api/analytics")
def api_analytics():
    events = zone_manager.get_event_log(500)
    by_type = {}; by_zone = {}; by_hour = {str(i): 0 for i in range(24)}
    for ev in events:
        t = ev.get("event", "unknown"); by_type[t] = by_type.get(t, 0) + 1
        z = ev.get("zone_name", "Unknown"); by_zone[z] = by_zone.get(z, 0) + 1
        try:
            h = datetime.fromisoformat(ev["timestamp"]).hour
            by_hour[str(h)] += 1
        except: pass
    cam_stats = {}
    for cid, e in cameras.items():
        s = e.get_stats()
        cam_stats[cid] = {
            "detections": s["detections_total"],
            "alerts": s["alerts_total"],
            "zone_alerts": s["zone_alerts"],
            "fps_history": e._fps_history[-30:]
        }
    return jsonify({
        "events_by_type": by_type, "events_by_zone": by_zone,
        "events_by_hour": by_hour, "camera_stats": cam_stats,
        "total_events": len(events)
    })

@app.route("/api/health")
def health():
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent
    except: cpu = 0; mem = 0
    yolo_ok = False
    try:
        yolo_ok = any(e.detector.strategy == "yolo" for e in cameras.values())
    except: pass
    return jsonify({
        "status": "ok", "uptime_sec": int(time.time() - _system_start),
        "cameras_total": len(cameras),
        "cameras_online": sum(1 for e in cameras.values() if e.get_frame() is not None),
        "cpu_pct": cpu, "mem_pct": mem,
        "zones_total": len(zone_manager.zones),
        "demo_mode": _settings["demo_mode"],
        "yolo_available": yolo_ok,
    })


# ── Notifications ────────────────────────────────────────────────

@app.route("/api/notifications")
def get_notifications():
    limit = int(request.args.get("limit", 30))
    return jsonify(list(reversed(_notif_log[-limit:])))

@app.route("/api/notifications/read", methods=["POST"])
def mark_read():
    nid = request.get_json(force=True).get("id")
    for n in _notif_log:
        if nid is None or n["id"] == nid: n["read"] = True
    return jsonify({"status": "ok"})

@app.route("/api/notifications/clear", methods=["POST"])
def clear_notifications():
    _notif_log.clear()
    return jsonify({"status": "ok"})


# ── Zones API ────────────────────────────────────────────────────

@app.route("/api/zones", methods=["GET"])
def get_zones(): return jsonify(zone_manager.get_all())

@app.route("/api/zones", methods=["POST"])
def create_zone():
    d = request.get_json(force=True)
    zone = Zone(
        id=d.get("id", str(uuid.uuid4())[:8]),
        name=d.get("name", "Zone"),
        points=d.get("points", []),
        capacity=int(d.get("capacity", 5)),
        risk_level=d.get("risk_level", "Medium"),
        rules=ZoneRule.from_dict(d.get("rules", {})),
        color=d.get("color", [0, 200, 100])
    )
    zone_manager.add_zone(zone)
    _push_notif("Zone Created", f'Zone "{zone.name}" is now monitoring', "info", zone=zone.name)
    return jsonify({"status": "created", "zone": zone.to_dict()}), 201

@app.route("/api/zones/<zid>", methods=["PUT"])
def update_zone(zid):
    ok = zone_manager.update_zone(zid, request.get_json(force=True))
    return jsonify({"status": "updated" if ok else "not_found"})

@app.route("/api/zones/<zid>", methods=["DELETE"])
def delete_zone(zid):
    ok = zone_manager.delete_zone(zid)
    if ok: _push_notif("Zone Deleted", f"Zone {zid} removed", "info")
    return jsonify({"status": "deleted" if ok else "not_found"})


# ── Settings API ──────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def get_settings():
    safe = {k: v for k, v in _settings.items() if k != "smtp_pass"}
    safe["smtp_pass_set"] = bool(_settings.get("smtp_pass"))  # just a flag, never expose password
    return jsonify(safe)

@app.route("/api/settings", methods=["POST"])
def update_settings():
    d = request.get_json(force=True)
    for k, v in d.items():
        if k not in _settings: continue
        if k == "smtp_pass":
            if v and not v.startswith("•"):
                _settings[k] = v
        else:
            _settings[k] = v
    alert_manager.cooldown_sec = _settings["alert_cooldown_sec"]
    _push_notif("Settings Updated", "Configuration saved successfully", "info")
    return jsonify({"status": "ok"})

@app.route("/api/test_email", methods=["POST"])
def test_email():
    d = request.get_json(force=True)
    override = d.get("recipient", "").strip()
    if override:
        _settings["email_recipient"] = override

    # Validate fields first
    if not _settings.get("smtp_user", "").strip():
        return jsonify({"status": "error", "message": "Username (Gmail ID) is empty — fill it in Settings"}), 400
    if not _settings.get("smtp_pass", "").strip():
        return jsonify({"status": "error", "message": "App Password is empty — fill it in Settings"}), 400
    if not _settings.get("email_recipient", "").strip():
        return jsonify({"status": "error", "message": "Recipient Email is empty — fill it in Settings"}), 400

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Run synchronously so we can return real success/failure
    ok, err = _do_send_email(
        subject="[HAWK] Test Alert Email",
        body_html=f"""
        <div style="font-family:system-ui,sans-serif;max-width:500px;margin:auto;padding:28px;background:#0d1117;border-radius:12px;color:#e8edf4;border:1px solid #1f2d3d;">
          <h2 style="color:#00c9ff;margin-top:0;">🦅 Hawk Surveillance — Test Email</h2>
          <p style="color:#e8edf4;">Your email alert configuration is working correctly.</p>
          <p style="color:#8b9ab0;font-size:12px;">Sent at: {ts}</p>
        </div>"""
    )
    if ok:
        return jsonify({"status": "sent", "message": f"Test email sent to {_settings['email_recipient']}"})
    else:
        return jsonify({"status": "error", "message": err}), 400


# ── Demo mode ────────────────────────────────────────────────────

@app.route("/api/demo/toggle", methods=["POST"])
def toggle_demo():
    _settings["demo_mode"] = not _settings["demo_mode"]
    state = "Enabled" if _settings["demo_mode"] else "Disabled"
    _push_notif(f"Demo Mode {state}",
                "Simulated data active" if _settings["demo_mode"] else "Live mode restored", "info")
    return jsonify({"demo_mode": _settings["demo_mode"]})


# ── Static files ──────────────────────────────────────────────────

@app.route("/frames/<f>")
def serve_frame(f): return send_from_directory("detected_frames", f)

@app.route("/clips/<f>")
def serve_clip(f): return send_from_directory("clips", f)

@app.route("/api/snapshots")
def snapshots():
    try:
        files = sorted([f for f in os.listdir("detected_frames") if f.endswith(".jpg")], reverse=True)
        return jsonify({"snapshots": files[:_settings["max_snapshots"]]})
    except: return jsonify({"snapshots": []})

@app.route("/api/snapshots/<fname>", methods=["DELETE"])
def delete_snapshot(fname):
    try:
        os.remove(os.path.join("detected_frames", fname))
        return jsonify({"status": "deleted"})
    except: return jsonify({"status": "not_found"}), 404


# ── Boot ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs("detected_frames", exist_ok=True)
    os.makedirs("clips", exist_ok=True)
    print("[HAWK] v5.0 starting on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)


# ── Camera diagnostics ────────────────────────────────────────────

@app.route("/api/camera/<cam_id>/status")
def cam_status(cam_id):
    """Detailed per-camera diagnostic status."""
    e = cameras.get(cam_id)
    if not e: return jsonify({"status": "not_found"}), 404
    frame = e.get_frame()
    s = e.get_stats()
    return jsonify({
        "cam_id": cam_id,
        "name": getattr(e, "meta", {}).get("name", cam_id),
        "source": getattr(e, "meta", {}).get("source", ""),
        "online": frame is not None,
        "fps": s.get("fps", 0),
        "reconnects": s.get("reconnects", 0),
        "detections_total": s.get("detections_total", 0),
        "recording": s.get("recording", False),
    })

@app.route("/api/camera/probe", methods=["POST"])
def cam_probe():
    """Quick connectivity test — can OpenCV open this source?"""
    import threading as _th
    d = request.get_json(force=True)
    src = d.get("source", "")
    try: src = int(src)
    except (ValueError, TypeError): pass

    result = {"reachable": False, "error": "timeout — source did not respond in 6s"}

    def _try():
        try:
            from modules.video_engine import _open_capture
            cap = _open_capture(src)
            if cap:
                ret, _ = cap.read()
                result["reachable"] = ret
                result["error"] = "" if ret else "Stream opened but no frame received"
                cap.release()
            else:
                result["error"] = "OpenCV could not open source"
        except Exception as ex:
            result["error"] = str(ex)

    t = _th.Thread(target=_try, daemon=True)
    t.start()
    t.join(timeout=7)
    return jsonify(result)
