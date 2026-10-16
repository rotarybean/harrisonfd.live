#!/usr/bin/env bash
# ============================================================
# capture.sh — RF capture pipeline
# Runs on capture node (OptiPlex 5060 / Ubuntu).
# Feeds rtl_fm → sox → timestamped WAV files in SPOOL_DIR.
# dispatch_agent.py handles rsync + DB insert after each close.
#
# All tuning values should be set via environment variables
# or edited directly after running tools/rtl_tuner.py.
# ============================================================

FREQ="${RTL_FREQ:-460.6375M}"
SAMPLE_RATE="${RTL_SAMPLE_RATE:-170k}"
GAIN="${RTL_GAIN:-0.0}"
PPM="${RTL_PPM:-0}"

SOX_SILENCE_ABOVE_DUR="${SOX_SILENCE_ABOVE_DUR:-0.5}"
SOX_SILENCE_ABOVE_THR="${SOX_SILENCE_ABOVE_THR:-22%}"
SOX_SILENCE_BELOW_DUR="${SOX_SILENCE_BELOW_DUR:-2.0}"
SOX_SILENCE_BELOW_THR="${SOX_SILENCE_BELOW_THR:-22%}"

SPOOL_DIR="${SPOOL_DIR:-/var/spool/dispatch}"

mkdir -p "$SPOOL_DIR"

cleanup() {
    echo "[capture] Signal received, stopping."
    kill "$SOX_PID" 2>/dev/null
    wait "$SOX_PID" 2>/dev/null
    exit 0
}
trap cleanup SIGTERM SIGINT

echo "[capture] Starting capture on $FREQ (gain=$GAIN, ppm=$PPM)"

rtl_fm \
    -f "$FREQ" \
    -M fm \
    -s "$SAMPLE_RATE" \
    -g "$GAIN" \
    -E dc \
    -E deemp \
    -p "$PPM" \
    2>/dev/null | \
sox -t raw -r "$SAMPLE_RATE" -e signed -b 16 -c 1 - \
    --no-show-progress \
    "$SPOOL_DIR/harrisonfr_$(date +%Y%m%d_%H%M%S).wav" \
    silence 1 "$SOX_SILENCE_ABOVE_DUR" "$SOX_SILENCE_ABOVE_THR" \
             1 "$SOX_SILENCE_BELOW_DUR" "$SOX_SILENCE_BELOW_THR" \
    : newfile : restart &

SOX_PID=$!
echo "[capture] sox PID: $SOX_PID"
wait "$SOX_PID"
