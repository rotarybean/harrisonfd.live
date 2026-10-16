#!/usr/bin/env bash
# Remove WAVs from local spool older than 1 hour.
# Add to crontab: */30 * * * * /bin/bash /opt/dispatch/scripts/spool_cleanup.sh >> /var/log/dispatch-cleanup.log 2>&1
SPOOL_DIR="${SPOOL_DIR:-/var/spool/dispatch}"
find "$SPOOL_DIR" -name "*.wav" -mmin +60 -delete
echo "[cleanup] $(date) spool cleaned"
