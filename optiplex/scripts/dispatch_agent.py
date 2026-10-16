#!/usr/bin/env python3
"""
dispatch_agent.py — HarrisonFD Dispatch Agent
Runs on capture node.

Watches SPOOL_DIR for closed WAV files via inotify.
For each new file:
  1. Runs sox stat to capture signal metrics
  2. rsync WAV to media server dated directory
  3. INSERT metadata row into Postgres on media server
     (transcript/priority left NULL — Whisper daemon fills those)
"""

import os
import re
import sys
import time
import wave
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import inotify.adapters
import inotify.constants
import psycopg2

# ── Config (all from environment) ───────────────────────────────────────────
SPOOL_DIR    = Path(os.environ.get("SPOOL_DIR",    "/var/spool/dispatch"))
REMOTE_USER  = os.environ.get("REMOTE_USER",  "dispatch")
REMOTE_HOST  = os.environ.get("MEDIA_SERVER_IP", "")
REMOTE_BASE  = os.environ.get("REMOTE_BASE",  "/data/dispatch")
SSH_KEY      = os.environ.get("SSH_KEY",      "")

DB_HOST = os.environ.get("PGHOST",     "")
DB_PORT = int(os.environ.get("PGPORT", 5432))
DB_NAME = os.environ.get("PGDATABASE", "dispatch")
DB_USER = os.environ.get("PGUSER",     "")
DB_PASS = os.environ.get("PGPASSWORD", "")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [agent] %(levelname)s %(message)s",
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


def get_signal_stats(path: Path) -> dict:
    stats = {
        "rms_amplitude":   None,
        "max_amplitude":   None,
        "mean_norm":       None,
        "rough_frequency": None,
    }
    try:
        result = subprocess.run(
            ["sox", str(path), "-n", "stat"],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stderr
        patterns = {
            "rms_amplitude":   r"RMS\s+amplitude:\s+([\d.]+)",
            "max_amplitude":   r"Maximum amplitude:\s+([\d.]+)",
            "mean_norm":       r"Mean\s+norm:\s+([\d.]+)",
            "rough_frequency": r"Rough\s+frequency:\s+([\d.]+)",
        }
        for key, pattern in patterns.items():
            m = re.search(pattern, output)
            if m:
                stats[key] = float(m.group(1))
        log.info(
            f"Signal: RMS={stats['rms_amplitude']} "
            f"MAX={stats['max_amplitude']} "
            f"FREQ={stats['rough_frequency']}Hz"
        )
    except Exception as e:
        log.warning(f"sox stat failed for {path.name}: {e}")
    return stats


def parse_timestamp(filename: str) -> datetime:
    try:
        stem  = Path(filename).stem
        parts = stem.split("_")
        return datetime.strptime(
            parts[1] + parts[2], "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except Exception:
        log.warning(f"Timestamp parse failed for {filename}, using now()")
        return datetime.now(timezone.utc)


def build_remote_path(recorded_at: datetime) -> str:
    return (
        f"{REMOTE_BASE}/"
        f"{recorded_at.year:04d}/"
        f"{recorded_at.month:02d}/"
        f"{recorded_at.day:02d}/"
    )


def rsync_file(local_path: Path, remote_path: str) -> bool:
    cmd = [
        "rsync", "-az", "--no-perms",
        "-e", f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=6",
        str(local_path),
        f"{REMOTE_USER}@{REMOTE_HOST}:{remote_path}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            log.info(f"rsync OK → {remote_path}{local_path.name}")
            return True
        else:
            log.error(f"rsync failed ({result.returncode}): {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        log.error(f"rsync timed out for {local_path.name}")
        return False
    except Exception as e:
        log.error(f"rsync exception for {local_path.name}: {e}")
        return False


def insert_transmission(conn, recorded_at, duration_sec, filename,
                        wav_path, signal_stats) -> int | None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO transmissions
                    (recorded_at, duration_sec, wav_filename, wav_path,
                     rms_amplitude, max_amplitude, mean_norm, rough_frequency)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    recorded_at, duration_sec, filename, wav_path,
                    signal_stats.get("rms_amplitude"),
                    signal_stats.get("max_amplitude"),
                    signal_stats.get("mean_norm"),
                    signal_stats.get("rough_frequency"),
                ),
            )
            row_id = cur.fetchone()[0]
        conn.commit()
        log.info(f"Inserted transmission #{row_id}: {filename} ({duration_sec}s)")
        return row_id
    except Exception as e:
        log.error(f"DB insert failed for {filename}: {e}")
        conn.rollback()
        return None


def process_wav(conn, wav_path: Path):
    if wav_path.stat().st_size < 1024:
        log.info(f"Skipping near-empty file: {wav_path.name}")
        return

    duration = get_wav_duration(wav_path)
    if duration < 0.5:
        log.info(f"Skipping very short segment ({duration}s): {wav_path.name}")
        return

    signal_stats = get_signal_stats(wav_path)
    recorded_at  = parse_timestamp(wav_path.name)
    remote_dir   = build_remote_path(recorded_at)
    remote_full  = f"{remote_dir}{wav_path.name}"

    if not rsync_file(wav_path, remote_dir):
        log.warning(f"rsync failed, skipping DB insert for {wav_path.name}")
        return

    insert_transmission(conn, recorded_at, duration,
                        wav_path.name, remote_full, signal_stats)


def main():
    if not all([REMOTE_HOST, SSH_KEY, DB_HOST, DB_USER, DB_PASS]):
        log.error("Missing required environment variables. Check your .env / service file.")
        sys.exit(1)

    SPOOL_DIR.mkdir(parents=True, exist_ok=True)

    log.info("Connecting to Postgres on media server …")
    conn = get_db()
    log.info("Connected.")

    notifier = inotify.adapters.Inotify()
    notifier.add_watch(str(SPOOL_DIR), mask=inotify.constants.IN_CLOSE_WRITE)
    log.info(f"Watching {SPOOL_DIR} for new WAV files …")

    try:
        for event in notifier.event_gen(yield_nones=False):
            (_, type_names, path, filename) = event
            if "IN_CLOSE_WRITE" not in type_names:
                continue
            if not filename.endswith(".wav"):
                continue

            wav_path = Path(path) / filename
            log.info(f"WAV closed: {wav_path.name}")
            time.sleep(0.2)

            try:
                process_wav(conn, wav_path)
            except psycopg2.OperationalError:
                log.warning("Lost DB connection, reconnecting …")
                try:
                    conn = get_db()
                    process_wav(conn, wav_path)
                except Exception as e2:
                    log.error(f"DB reconnect failed: {e2}")

    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
