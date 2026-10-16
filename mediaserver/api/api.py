#!/usr/bin/env python3
"""
api.py — HarrisonFD Dispatch Monitor REST API
Runs on media server, port 5001.

All configuration via environment variables — see .env.example
"""

import os
import math
from pathlib import Path
from flask import Flask, jsonify, request, send_file
import psycopg2
import psycopg2.extras

app = Flask(__name__)

DB_CONFIG = dict(
    host     = os.environ.get("PGHOST",     "localhost"),
    port     = int(os.environ.get("PGPORT", 5432)),
    dbname   = os.environ.get("PGDATABASE", "dispatch"),
    user     = os.environ.get("PGUSER",     ""),
    password = os.environ.get("PGPASSWORD", ""),
)


def get_db():
    conn = psycopg2.connect(**DB_CONFIG, connect_timeout=5)
    conn.autocommit = False
    return conn


def row_to_dict(row) -> dict:
    d = dict(row)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif hasattr(v, "__float__"):
            d[k] = round(float(v), 2)
    return d


def db_error(e):
    app.logger.error(f"DB error: {e}")
    return jsonify({"error": "database error"}), 500


@app.route("/api/health")
def health():
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transmissions")
            total = cur.fetchone()[0]
        conn.close()
        return jsonify({"status": "ok", "db": "ok", "total_transmissions": total})
    except Exception as e:
        return jsonify({"status": "degraded", "db": str(e)}), 503


@app.route("/api/stats")
def stats():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM live_stats")
            row = cur.fetchone()
        conn.close()
        return jsonify(row_to_dict(row) if row else {})
    except Exception as e:
        return db_error(e)


@app.route("/api/stats/daily")
def stats_daily():
    limit = min(90, int(request.args.get("days", 30)))
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM daily_summary LIMIT %s", (limit,))
            rows = [row_to_dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify(rows)
    except Exception as e:
        return db_error(e)


@app.route("/api/transmissions")
def list_transmissions():
    page     = max(1, int(request.args.get("page", 1)))
    per_page = min(200, max(1, int(request.args.get("per_page", 50))))
    q        = request.args.get("q", "").strip()
    date     = request.args.get("date", "").strip()
    pri      = request.args.get("priority", "").strip()
    offset   = (page - 1) * per_page

    where, params = [], []
    if q:
        where.append("to_tsvector('english', COALESCE(transcript,'')) @@ plainto_tsquery('english', %s)")
        params.append(q)
    if date:
        where.append("DATE(recorded_at AT TIME ZONE 'UTC') = %s")
        params.append(date)
    if pri in ("routine", "elevated", "high"):
        where.append("priority = %s::priority_level")
        params.append(pri)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM transmissions {where_sql}", params)
            total = cur.fetchone()["n"]
            cur.execute(
                f"""
                SELECT id, recorded_at, duration_sec, wav_filename, wav_path,
                       transcript, whisper_model, priority, llm_summary, analyzed, created_at
                FROM transmissions {where_sql}
                ORDER BY recorded_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [per_page, offset],
            )
            rows = [row_to_dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({
            "transmissions": rows, "total": total,
            "page": page, "per_page": per_page,
            "pages": max(1, math.ceil(total / per_page)),
        })
    except Exception as e:
        return db_error(e)


@app.route("/api/transmissions/<int:tx_id>")
def get_transmission(tx_id):
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM transmissions WHERE id = %s", (tx_id,))
            row = cur.fetchone()
        conn.close()
        if not row: return jsonify({"error": "not found"}), 404
        return jsonify(row_to_dict(row))
    except Exception as e:
        return db_error(e)


@app.route("/api/transmissions/<int:tx_id>/audio")
def get_audio(tx_id):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT wav_path FROM transmissions WHERE id = %s", (tx_id,))
            row = cur.fetchone()
        conn.close()
        if not row: return jsonify({"error": "not found"}), 404
        wav = Path(row[0])
        if not wav.exists(): return jsonify({"error": "audio file not on disk"}), 404
        return send_file(str(wav), mimetype="audio/wav", as_attachment=False)
    except Exception as e:
        return db_error(e)


@app.route("/api/alerts")
def list_alerts():
    acked = request.args.get("acknowledged", "false").lower() == "true"
    limit = min(500, int(request.args.get("limit", 100)))
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT a.*, t.recorded_at, t.wav_filename, t.transcript,
                       kr.category, kr.keyword AS rule_keyword
                FROM alerts a
                JOIN transmissions t ON a.transmission_id = t.id
                LEFT JOIN keyword_rules kr ON a.keyword_rule_id = kr.id
                WHERE a.acknowledged = %s
                ORDER BY a.created_at DESC LIMIT %s
                """,
                (acked, limit),
            )
            rows = [row_to_dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify(rows)
    except Exception as e:
        return db_error(e)


@app.route("/api/alerts/<int:alert_id>/acknowledge", methods=["POST"])
def acknowledge_alert(alert_id):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("UPDATE alerts SET acknowledged = TRUE WHERE id = %s RETURNING id", (alert_id,))
            row = cur.fetchone()
        conn.commit(); conn.close()
        if not row: return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True, "alert_id": alert_id})
    except Exception as e:
        return db_error(e)


@app.route("/api/keyword-rules")
def list_rules():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM keyword_rules ORDER BY category, keyword")
            rows = [row_to_dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify(rows)
    except Exception as e:
        return db_error(e)


@app.route("/api/keyword-rules", methods=["POST"])
def create_rule():
    data        = request.get_json(force=True, silent=True) or {}
    keyword     = (data.get("keyword") or "").strip().lower()
    category    = (data.get("category") or "").strip()
    alert_level = data.get("alert_level", "elevated")
    is_active   = bool(data.get("is_active", True))
    if not keyword or not category:
        return jsonify({"error": "keyword and category required"}), 400
    if alert_level not in ("routine", "elevated", "high"):
        return jsonify({"error": "alert_level must be routine, elevated, or high"}), 400
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO keyword_rules (keyword, category, alert_level, is_active) VALUES (%s,%s,%s::priority_level,%s) RETURNING *",
                (keyword, category, alert_level, is_active),
            )
            row = row_to_dict(cur.fetchone())
        conn.commit(); conn.close()
        return jsonify(row), 201
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": f"keyword '{keyword}' already exists"}), 409
    except Exception as e:
        return db_error(e)


@app.route("/api/keyword-rules/<int:rule_id>", methods=["PUT"])
def update_rule(rule_id):
    data = request.get_json(force=True, silent=True) or {}
    fields, params = [], []
    if "keyword"     in data: fields.append("keyword = %s");                  params.append(data["keyword"].strip().lower())
    if "category"    in data: fields.append("category = %s");                 params.append(data["category"].strip())
    if "alert_level" in data: fields.append("alert_level = %s::priority_level"); params.append(data["alert_level"])
    if "is_active"   in data: fields.append("is_active = %s");                params.append(bool(data["is_active"]))
    if not fields: return jsonify({"error": "no fields to update"}), 400
    params.append(rule_id)
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"UPDATE keyword_rules SET {', '.join(fields)} WHERE id = %s RETURNING *", params)
            row = cur.fetchone()
        conn.commit(); conn.close()
        if not row: return jsonify({"error": "not found"}), 404
        return jsonify(row_to_dict(row))
    except Exception as e:
        return db_error(e)


@app.route("/api/keyword-rules/<int:rule_id>", methods=["DELETE"])
def delete_rule(rule_id):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM keyword_rules WHERE id = %s RETURNING id", (rule_id,))
            row = cur.fetchone()
        conn.commit(); conn.close()
        if not row: return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True, "deleted_id": rule_id})
    except Exception as e:
        return db_error(e)


@app.errorhandler(404)
def not_found(e): return jsonify({"error": "not found"}), 404

@app.errorhandler(500)
def server_error(e): return jsonify({"error": "internal server error"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
