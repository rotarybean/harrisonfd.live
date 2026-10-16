#!/usr/bin/env python3
"""
llm_service.py — HarrisonFD LLM analysis service
Runs on media server, port 5002 (localhost only — not exposed to LAN).

Receives transcript from Whisper daemon and:
  1. Scans for active keyword rules → inserts alerts
  2. Sends transcript to LLM for priority scoring + summary
  3. UPDATEs transmissions.priority, llm_summary, analyzed = TRUE
  4. For HIGH keyword hits, asks LLM for alert narrative

Degrades gracefully — keyword matching runs even if LLM is unreachable.

All configuration via environment variables — see .env.example
"""

import os
import json
import logging
import threading
import queue
from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras
import requests

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [llm-svc] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
DB_HOST = os.environ.get("PGHOST",     "localhost")
DB_PORT = int(os.environ.get("PGPORT", 5432))
DB_NAME = os.environ.get("PGDATABASE", "dispatch")
DB_USER = os.environ.get("PGUSER",     "")
DB_PASS = os.environ.get("PGPASSWORD", "")

LLM_ENDPOINT = os.environ.get("LLM_ENDPOINT", "")
LLM_MODEL    = os.environ.get("LLM_MODEL",    "llama3")
LLM_ENABLED  = os.environ.get("LLM_ENABLED",  "true").lower() == "true"
LLM_TIMEOUT  = int(os.environ.get("LLM_TIMEOUT", 30))

PRIORITY_RANK = {"routine": 0, "elevated": 1, "high": 2}
# ─────────────────────────────────────────────────────────────────────────────

work_queue: queue.Queue = queue.Queue()


def get_db():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS, connect_timeout=8,
    )


def llm_call(prompt: str) -> str | None:
    if not LLM_ENABLED or not LLM_ENDPOINT:
        return None
    try:
        r = requests.post(
            LLM_ENDPOINT,
            json={"model": LLM_MODEL, "prompt": prompt, "stream": False},
            timeout=LLM_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        log.warning(f"LLM call failed: {e}")
        return None


def llm_score_priority(transcript: str) -> tuple[str, str]:
    prompt = f"""You are a 911 dispatch monitor analyzing radio transcripts.

Transcript: "{transcript}"

Respond ONLY with a JSON object in this exact format, no other text:
{{
  "priority": "routine" | "elevated" | "high",
  "summary": "one sentence summary of the event"
}}

Priority definitions:
- "routine": administrative, status checks, minor incidents
- "elevated": incidents requiring response but not immediately life-threatening
- "high": life safety emergencies — cardiac arrest, structure fire, shooting, major MVA with injuries, etc.
"""
    response = llm_call(prompt)
    if not response:
        return "routine", ""
    try:
        clean = response.strip().lstrip("```json").rstrip("```").strip()
        data  = json.loads(clean)
        priority = data.get("priority", "routine")
        if priority not in PRIORITY_RANK:
            priority = "routine"
        return priority, data.get("summary", "").strip()
    except Exception as e:
        log.warning(f"LLM priority parse failed: {e}")
        return "routine", ""


def llm_alert_response(transcript: str, keyword: str, category: str) -> str:
    prompt = f"""You are a 911 dispatch monitor. A high-priority keyword was detected.

Keyword: "{keyword}" (category: {category})
Transcript: "{transcript}"

Write a concise 1-2 sentence alert summary for the dispatcher monitoring this feed.
Focus on actionable information. No preamble.
"""
    return llm_call(prompt) or ""


def load_keyword_rules(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, keyword, category, alert_level FROM keyword_rules WHERE is_active = TRUE")
        return [dict(r) for r in cur.fetchall()]


def scan_keywords(transcript: str, rules: list[dict]) -> list[dict]:
    lower = transcript.lower()
    return [r for r in rules if r["keyword"].lower() in lower]


def insert_alert(conn, tx_id: int, rule: dict, llm_response: str = ""):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO alerts (transmission_id, keyword_rule_id, keyword_matched, alert_level, llm_response) VALUES (%s,%s,%s,%s::priority_level,%s)",
            (tx_id, rule["id"], rule["keyword"], rule["alert_level"], llm_response or None),
        )
    conn.commit()
    log.info(f"Alert: tx#{tx_id} keyword='{rule['keyword']}' level={rule['alert_level']}")


def update_transmission(conn, tx_id: int, priority: str, llm_summary: str):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE transmissions SET priority=%s::priority_level, llm_summary=%s, analyzed=TRUE WHERE id=%s",
            (priority, llm_summary or None, tx_id),
        )
    conn.commit()
    log.info(f"tx#{tx_id} priority={priority}")


def analyze(tx_id: int, transcript: str, wav_path: str):
    if not transcript:
        try:
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute("UPDATE transmissions SET priority='routine', analyzed=TRUE WHERE id=%s", (tx_id,))
            conn.commit(); conn.close()
        except Exception as e:
            log.error(f"DB error marking empty tx#{tx_id}: {e}")
        return

    try:
        conn = get_db()
    except Exception as e:
        log.error(f"DB connect failed for tx#{tx_id}: {e}")
        return

    try:
        rules   = load_keyword_rules(conn)
        matches = scan_keywords(transcript, rules)

        kw_priority = "routine"
        for m in matches:
            if PRIORITY_RANK.get(m["alert_level"], 0) > PRIORITY_RANK.get(kw_priority, 0):
                kw_priority = m["alert_level"]

        llm_priority, llm_summary = llm_score_priority(transcript)

        final_priority = (
            kw_priority
            if PRIORITY_RANK.get(kw_priority, 0) >= PRIORITY_RANK.get(llm_priority, 0)
            else llm_priority
        )

        for m in matches:
            alert_response = ""
            if m["alert_level"] == "high" and LLM_ENABLED:
                alert_response = llm_alert_response(transcript, m["keyword"], m["category"])
            insert_alert(conn, tx_id, m, alert_response)

        update_transmission(conn, tx_id, final_priority, llm_summary)

    except Exception as e:
        log.error(f"Analysis error for tx#{tx_id}: {e}")
    finally:
        conn.close()


def worker():
    log.info("LLM analysis worker thread started.")
    while True:
        job = work_queue.get()
        if job is None: break
        try:
            analyze(**job)
        except Exception as e:
            log.error(f"Unhandled worker error: {e}")
        finally:
            work_queue.task_done()


threading.Thread(target=worker, daemon=True, name="llm-worker").start()


@app.route("/internal/analyze", methods=["POST"])
def internal_analyze():
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"error": "invalid JSON"}), 400
    tx_id      = data.get("transmission_id")
    transcript = data.get("transcript", "")
    wav_path   = data.get("wav_path", "")
    if not tx_id: return jsonify({"error": "transmission_id required"}), 400
    work_queue.put({"tx_id": tx_id, "transcript": transcript, "wav_path": wav_path})
    log.info(f"Queued analysis for tx#{tx_id} (queue depth: {work_queue.qsize()})")
    return jsonify({"queued": True, "tx_id": tx_id}), 200


@app.route("/internal/health")
def health():
    return jsonify({
        "status":      "ok",
        "llm_enabled": LLM_ENABLED,
        "llm_model":   LLM_MODEL,
        "queue_depth": work_queue.qsize(),
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5002, debug=False)
