#!/usr/bin/env python3
"""
whisper_daemon.py — HarrisonFD Whisper transcription daemon
Runs on media server.

Watches DISPATCH_WATCH_ROOT recursively for new WAV files (inotify).
For each new WAV:
  1. Transcribes with Whisper
  2. UPDATEs the transmissions row (capture node already INSERTed it)
  3. POSTs to LLM service for priority scoring and keyword scan

All configuration via environment variables — see .env.example
"""

import os
import sys
import time
import wave
import logging
import requests
from pathlib import Path

import whisper
import psycopg2
import inotify.adapters
import inotify.constants

# ── Config ───────────────────────────────────────────────────────────────────
WATCH_ROOT    = Path(os.environ.get("DISPATCH_WATCH_ROOT",   "/data/dispatch"))
WHISPER_MODEL = os.environ.get("DISPATCH_WHISPER_MODEL",     "base.en")

DB_HOST = os.environ.get("PGHOST",     "localhost")
DB_PORT = int(os.environ.get("PGPORT", 5432))
DB_NAME = os.environ.get("PGDATABASE", "dispatch")
DB_USER = os.environ.get("PGUSER",     "")
DB_PASS = os.environ.get("PGPASSWORD", "")

LLM_SERVICE_URL = os.environ.get("LLM_SERVICE_URL", "http://localhost:5002/internal/analyze")
LOG_LEVEL       = os.environ.get("LOG_LEVEL", "INFO")
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [whisper] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def get_db():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS, connect_timeout=8,
    )


def get_wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            return round(wf.getnframes() / float(wf.getframerate()), 2)
    except Exception as e:
        log.warning(f"Duration read failed {path.name}: {e}")
        return 0.0


def update_transcript(conn, wav_path: str, transcript: str, model: str):
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE transmissions SET transcript = %s, whisper_model = %s WHERE wav_path = %s RETURNING id",
                (transcript, model, wav_path),
            )
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception as e:
        log.error(f"DB update failed for {wav_path}: {e}")
        conn.rollback()
        return None


def transcribe(model, path: Path) -> str:
    log.info(f"Transcribing {path.name} …")
    result = model.transcribe(str(path), language="en", fp16=False)
    text = result.get("text", "").strip()
    log.info(f"  → {text[:80]}{'…' if len(text) > 80 else ''}")
    return text


def trigger_llm(tx_id: int, transcript: str, wav_path: str):
    try:
        r = requests.post(
            LLM_SERVICE_URL,
            json={"transmission_id": tx_id, "transcript": transcript, "wav_path": wav_path},
            timeout=5,
        )
        if r.status_code == 200:
            log.info(f"LLM service accepted tx#{tx_id}")
        else:
            log.warning(f"LLM service returned {r.status_code} for tx#{tx_id}")
    except Exception as e:
        log.warning(f"LLM service unreachable for tx#{tx_id}: {e}")


def process_wav(model, conn, wav_path: Path):
    duration = get_wav_duration(wav_path)
    if duration < 0.5:
        log.info(f"Skipping very short file ({duration}s): {wav_path.name}")
        return

    try:
        transcript = transcribe(model, wav_path)
    except Exception as e:
        log.error(f"Whisper failed on {wav_path.name}: {e}")
        transcript = ""

    tx_id = update_transcript(conn, str(wav_path), transcript, WHISPER_MODEL)

    if tx_id is None:
        log.warning(f"No DB row found for {wav_path.name}, retrying in 3s")
        time.sleep(3)
        tx_id = update_transcript(conn, str(wav_path), transcript, WHISPER_MODEL)

    if tx_id:
        trigger_llm(tx_id, transcript, str(wav_path))
    else:
        log.error(f"Could not find DB row for {wav_path.name} — transcript not saved")


def main():
    if not all([DB_USER, DB_PASS]):
        log.error("Missing PGUSER or PGPASSWORD environment variables.")
        sys.exit(1)

    log.info(f"Loading Whisper model: {WHISPER_MODEL}")
    model = whisper.load_model(WHISPER_MODEL)
    log.info("Whisper ready.")

    log.info("Connecting to Postgres …")
    conn = get_db()
    log.info("Connected.")

    WATCH_ROOT.mkdir(parents=True, exist_ok=True)
    notifier = inotify.adapters.InotifyTree(
        str(WATCH_ROOT),
        mask=inotify.constants.IN_CLOSE_WRITE | inotify.constants.IN_CREATE,
    )
    log.info(f"Watching {WATCH_ROOT} recursively …")

    try:
        for event in notifier.event_gen(yield_nones=False):
            (_, type_names, watch_path, filename) = event
            if "IN_CLOSE_WRITE" not in type_names: continue
            if not filename.endswith(".wav"): continue

            wav_path = Path(watch_path) / filename
            log.info(f"New WAV: {wav_path}")
            time.sleep(0.3)

            try:
                process_wav(model, conn, wav_path)
            except psycopg2.OperationalError:
                log.warning("DB connection lost, reconnecting …")
                try:
                    conn = get_db()
                    process_wav(model, conn, wav_path)
                except Exception as e2:
                    log.error(f"DB reconnect failed: {e2}")

    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
