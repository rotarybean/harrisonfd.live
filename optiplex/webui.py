#!/usr/bin/env python3
"""
webui.py — HarrisonFD Dispatch Monitor Web UI
Runs on capture node, port 8080 (Nginx proxies to public).

Reads directly from Postgres on media server.
Proxies audio through the REST API on media server.
Polls /api/stats every 30s for alert badge updates.
Auto-play queue polls /api/latest every 5s for new transmissions.
"""

import os
import math
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, Response, jsonify
import psycopg2
import psycopg2.extras
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [webui] %(message)s")
log = logging.getLogger(__name__)

# ── Config (all from environment) ────────────────────────────────────────────
DB_CONFIG = dict(
    host     = os.environ.get("PGHOST",     ""),
    port     = int(os.environ.get("PGPORT", 5432)),
    dbname   = os.environ.get("PGDATABASE", "dispatch"),
    user     = os.environ.get("PGUSER",     ""),
    password = os.environ.get("PGPASSWORD", ""),
)
API_BASE  = os.environ.get("DISPATCH_API_BASE", "")
PAGE_SIZE = 50
TZ        = ZoneInfo(os.environ.get("DISPLAY_TZ", "America/Chicago"))
# ─────────────────────────────────────────────────────────────────────────────


def get_db():
    return psycopg2.connect(**DB_CONFIG, connect_timeout=5)


def db_fetchall(sql, params=()):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def db_fetchone(sql, params=()):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def serialize(obj):
    out = {}
    for k, v in obj.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif v is None:
            out[k] = None
        else:
            try:
                out[k] = float(v) if "." in str(v) else v
            except (TypeError, ValueError):
                out[k] = v
    return out


def record_visit():
    today = datetime.now(TZ).date()
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO visitor_counts (date, count)
                VALUES (%s, 1)
                ON CONFLICT (date) DO UPDATE
                    SET count = visitor_counts.count + 1
                """,
                (today,),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"Visitor count error: {e}")


def get_visitor_counts() -> dict:
    today = datetime.now(TZ).date()
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT count FROM visitor_counts WHERE date = %s", (today,))
            row = cur.fetchone()
            today_count = row[0] if row else 0
            cur.execute("SELECT total_visitors()")
            total = cur.fetchone()[0]
        conn.close()
        return {"today": today_count, "total": total}
    except Exception as e:
        log.error(f"Visitor fetch error: {e}")
        return {"today": 0, "total": 0}


@app.route("/")
def index():
    record_visit()

    page   = max(1, int(request.args.get("page", 1)))
    q      = request.args.get("q", "").strip()
    date   = request.args.get("date", "").strip()
    pri    = request.args.get("priority", "").strip()
    offset = (page - 1) * PAGE_SIZE

    where, params = [], []
    if q:
        where.append("to_tsvector('english', COALESCE(t.transcript,'')) @@ plainto_tsquery('english', %s)")
        params.append(q)
    if date:
        where.append("DATE(t.recorded_at AT TIME ZONE 'UTC') = %s")
        params.append(date)
    if pri in ("routine", "elevated", "high"):
        where.append("t.priority = %s::priority_level")
        params.append(pri)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total_row = db_fetchone(f"SELECT COUNT(*) AS n FROM transmissions t {where_sql}", params)
    total     = total_row["n"] if total_row else 0
    pages     = max(1, math.ceil(total / PAGE_SIZE))

    rows = db_fetchall(
        f"""
        SELECT t.id, t.recorded_at, t.duration_sec, t.wav_filename,
               t.transcript, t.priority, t.analyzed, t.llm_summary,
               t.rms_amplitude, t.max_amplitude, t.rough_frequency
        FROM transmissions t
        {where_sql}
        ORDER BY t.recorded_at DESC
        LIMIT %s OFFSET %s
        """,
        params + [PAGE_SIZE, offset],
    )

    stats     = db_fetchone("SELECT * FROM live_stats") or {}
    latest    = db_fetchone("SELECT id FROM transmissions ORDER BY recorded_at DESC LIMIT 1")
    latest_id = latest["id"] if latest else 0
    visitors  = get_visitor_counts()

    return render_template(
        "index.html",
        transmissions=rows,
        total=total, page=page, pages=pages,
        search=q, date=date, priority=pri,
        stats=stats,
        latest_id=latest_id,
        visitors=visitors,
    )


@app.route("/transmission/<int:tx_id>")
def detail(tx_id):
    tx = db_fetchone("SELECT * FROM transmissions WHERE id = %s", (tx_id,))
    if not tx:
        return "Not found", 404
    alerts = db_fetchall(
        """
        SELECT a.*, kr.category
        FROM alerts a
        LEFT JOIN keyword_rules kr ON a.keyword_rule_id = kr.id
        WHERE a.transmission_id = %s
        ORDER BY a.created_at
        """,
        (tx_id,),
    )
    return render_template("detail.html", tx=tx, alerts=alerts)


@app.route("/alerts")
def alerts_page():
    unacked = db_fetchall(
        """
        SELECT a.*, t.recorded_at, t.wav_filename, t.transcript,
               kr.category, kr.keyword
        FROM alerts a
        JOIN transmissions t ON a.transmission_id = t.id
        LEFT JOIN keyword_rules kr ON a.keyword_rule_id = kr.id
        WHERE a.acknowledged = FALSE
        ORDER BY a.created_at DESC
        LIMIT 200
        """,
    )
    return render_template("alerts.html", alerts=unacked)


@app.route("/alerts/<int:alert_id>/ack", methods=["POST"])
def ack_alert(alert_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE alerts SET acknowledged = TRUE WHERE id = %s", (alert_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/stats")
def stats_json():
    stats = db_fetchone("SELECT * FROM live_stats") or {}
    return jsonify({k: (int(v) if v is not None else 0) for k, v in stats.items()})


@app.route("/api/latest")
def latest_transmissions():
    after = int(request.args.get("after", 0))
    rows  = db_fetchall(
        """
        SELECT id, recorded_at, duration_sec, wav_filename,
               transcript, priority,
               rms_amplitude, max_amplitude, rough_frequency
        FROM transmissions
        WHERE id > %s
        ORDER BY id ASC
        LIMIT 10
        """,
        (after,),
    )
    return jsonify([serialize(r) for r in rows])


@app.route("/api/visitors")
def visitors_json():
    return jsonify(get_visitor_counts())


@app.route("/audio/<int:tx_id>")
def audio_proxy(tx_id):
    try:
        r = requests.get(
            f"{API_BASE}/transmissions/{tx_id}/audio",
            stream=True, timeout=10,
        )
        r.raise_for_status()
        return Response(
            r.iter_content(chunk_size=8192),
            content_type="audio/wav",
            headers={"Accept-Ranges": "bytes"},
        )
    except Exception as e:
        log.error(f"Audio proxy error tx#{tx_id}: {e}")
        return "Audio unavailable", 502


@app.route("/health")
def health():
    try:
        db_fetchone("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return jsonify({"webui": "ok", "db": "ok" if db_ok else "error"}), 200 if db_ok else 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
