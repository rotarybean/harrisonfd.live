#!/usr/bin/env python3
"""
rtl_tuner.py — Standalone RTL-SDR tuning tool for harrisonfd-dispatch
Completely independent of dispatch services.

Streams live audio to Icecast → VLC (or any stream player) while allowing
real-time adjustment of all SDR parameters via keypresses.

Prerequisites:
  - icecast2 running (sudo systemctl start icecast2)
  - Set ICECAST_SOURCE_PASSWORD in environment or edit DEFAULTS below
  - rtl-sdr and ffmpeg installed

Usage:
  python3 tools/rtl_tuner.py

Stream URL: http://your-capture-node:8000/tuner

Controls:
  p / P     PPM correction -1 / +1
  g / G     Gain -1 / +1
  h / H     Highpass frequency -50 / +50 Hz
  l / L     Lowpass frequency -50 / +50 Hz
  n / N     Notch frequency -10 / +10 Hz
  v / V     Volume -5 / +5
  s         Cycle sample rate (170k → 200k → 250k)
  d         Toggle de-emphasis
  0         Reset to defaults
  S         Save current settings to tuner_settings.txt
  q         Quit
"""

import os
import sys
import tty
import termios
import subprocess
import threading
import time
from datetime import datetime

DEFAULTS = {
    "freq":        os.environ.get("RTL_FREQ", "460.6375M"),
    "ppm":         int(os.environ.get("RTL_PPM", "0")),
    "gain":        float(os.environ.get("RTL_GAIN", "0.0")),
    "sample_rate": int(os.environ.get("RTL_SAMPLE_RATE", "170000")),
    "highpass":    400,
    "lowpass":     3500,
    "notch":       1593.75,
    "volume":      40,
    "deemp":       True,
}

SAMPLE_RATES = [170000, 200000, 250000]

_icecast_pw  = os.environ.get("ICECAST_SOURCE_PASSWORD", "")
if not _icecast_pw:
    print("[ERROR] Set ICECAST_SOURCE_PASSWORD in your environment.")
    sys.exit(1)
ICECAST_URL  = f"icecast://source:{_icecast_pw}@localhost:8000/tuner"

settings = dict(DEFAULTS)
rtl_proc = None
ff_proc  = None
running  = True
lock     = threading.Lock()


def build_audio_filter():
    return ",".join([
        f"highpass=f={settings['highpass']}",
        f"lowpass=f={settings['lowpass']}",
        f"volume={settings['volume']}",
        f"equalizer=f={settings['notch']:.2f}:width_type=o:width=0.5:g=-30",
    ])


def build_rtl_cmd():
    cmd = [
        "rtl_fm", "-f", settings["freq"], "-M", "fm",
        "-s", str(settings["sample_rate"]),
        "-g", str(settings["gain"]),
        "-E", "dc", "-p", str(settings["ppm"]),
    ]
    if settings["deemp"]:
        cmd += ["-E", "deemp"]
    return cmd


def build_ff_cmd():
    return [
        "ffmpeg", "-f", "s16le",
        "-ar", str(settings["sample_rate"]), "-ac", "1", "-i", "pipe:0",
        "-af", build_audio_filter(),
        "-acodec", "libmp3lame", "-ab", "32k", "-ar", "22050",
        "-f", "mp3", ICECAST_URL,
    ]


def stop_pipeline():
    global rtl_proc, ff_proc
    for p in (rtl_proc, ff_proc):
        if p:
            try: p.terminate(); p.wait(timeout=3)
            except Exception:
                try: p.kill()
                except: pass
    rtl_proc = ff_proc = None
    time.sleep(0.5)


def start_pipeline():
    global rtl_proc, ff_proc
    with lock:
        stop_pipeline()
        try:
            rtl_proc = subprocess.Popen(build_rtl_cmd(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            ff_proc  = subprocess.Popen(build_ff_cmd(), stdin=rtl_proc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            rtl_proc.stdout.close()
        except Exception as e:
            print(f"\n[ERROR] Failed to start pipeline: {e}")


def restart_pipeline():
    threading.Thread(target=start_pipeline, daemon=True).start()


def save_settings():
    fname = os.path.expanduser("~/tuner_settings.txt")
    with open(fname, "w") as f:
        f.write(f"# RTL Tuner settings saved {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Paste these into optiplex/scripts/capture.sh\n\n")
        f.write(f"FREQ=\"{settings['freq']}\"\n")
        f.write(f"SAMPLE_RATE=\"{settings['sample_rate']}\"\n")
        f.write(f"GAIN=\"{settings['gain']}\"\n")
        f.write(f"PPM=\"{settings['ppm']}\"\n")
        f.write(f"\n# ffmpeg audio filter chain:\n# {build_audio_filter()}\n")
        f.write(f"\n# rtl_fm command:\n# {' '.join(build_rtl_cmd())}\n")
    print(f"\n[SAVED] Settings written to {fname}")


def print_status():
    os.system("clear")
    print("=" * 58)
    print("  RTL-SDR TUNER  —  harrisonfd-dispatch")
    print("=" * 58)
    print(f"  Stream:      http://localhost:8000/tuner")
    print(f"  Frequency:   {settings['freq']}")
    print(f"  PPM:         {settings['ppm']:>6}        [p/P] -1/+1")
    print(f"  Gain:        {settings['gain']:>6}        [g/G] -1/+1")
    print(f"  Sample rate: {settings['sample_rate']:>6}        [s]   cycle")
    print(f"  Highpass:    {settings['highpass']:>6} Hz    [h/H] -50/+50")
    print(f"  Lowpass:     {settings['lowpass']:>6} Hz    [l/L] -50/+50")
    print(f"  Notch:       {settings['notch']:>9.2f} Hz [n/N] -10/+10")
    print(f"  Volume:      {settings['volume']:>6}        [v/V] -5/+5")
    print(f"  De-emphasis: {'ON' if settings['deemp'] else 'OFF':>6}        [d]   toggle")
    print("=" * 58)
    print("  [0] Reset   [S] Save   [q] Quit")
    print("=" * 58)


def get_key():
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def handle_key(key) -> bool:
    changed = True
    if   key == 'p': settings["ppm"]         -= 1
    elif key == 'P': settings["ppm"]         += 1
    elif key == 'g': settings["gain"]         = max(0, settings["gain"] - 1)
    elif key == 'G': settings["gain"]        += 1
    elif key == 'h': settings["highpass"]     = max(50,   settings["highpass"] - 50)
    elif key == 'H': settings["highpass"]     = min(2000, settings["highpass"] + 50)
    elif key == 'l': settings["lowpass"]      = max(1000, settings["lowpass"] - 50)
    elif key == 'L': settings["lowpass"]      = min(8000, settings["lowpass"] + 50)
    elif key == 'n': settings["notch"]        = max(100,  settings["notch"] - 10)
    elif key == 'N': settings["notch"]       += 10
    elif key == 'v': settings["volume"]       = max(1,    settings["volume"] - 5)
    elif key == 'V': settings["volume"]      += 5
    elif key == 'd': settings["deemp"]        = not settings["deemp"]
    elif key == 's':
        idx = SAMPLE_RATES.index(settings["sample_rate"])
        settings["sample_rate"] = SAMPLE_RATES[(idx + 1) % len(SAMPLE_RATES)]
    elif key == '0': settings.update(DEFAULTS)
    elif key == 'S': save_settings(); changed = False
    elif key in ('q', '\x03'): return False
    else: changed = False

    if changed:
        print_status()
        restart_pipeline()
    return True


def monitor_pipeline():
    while running:
        time.sleep(5)
        with lock:
            if ff_proc and ff_proc.poll() is not None:
                print("\n[INFO] Pipeline died, restarting...")
                start_pipeline()


def main():
    global running
    result = subprocess.run(["systemctl", "is-active", "icecast2"], capture_output=True, text=True)
    if result.stdout.strip() != "active":
        print("[ERROR] Icecast2 is not running.")
        print("  sudo systemctl start icecast2")
        sys.exit(1)

    print_status()
    print("  Starting pipeline...")
    start_pipeline()
    time.sleep(2)
    print_status()

    threading.Thread(target=monitor_pipeline, daemon=True).start()

    try:
        while True:
            key = get_key()
            if not handle_key(key):
                break
    except Exception as e:
        print(f"\n[ERROR] {e}")
    finally:
        running = False
        print("\n[INFO] Stopping pipeline...")
        stop_pipeline()
        print("[INFO] Done.")


if __name__ == "__main__":
    main()
