#!/usr/bin/env bash
#
# Roll the punches the plant agent copied in up into the attendance register.
#
# The second half of the pipeline, and the half that runs on this server. The
# first half is `attendance_sync.py` on a Windows box inside the plant, which
# copies raw punches into `attendance_punchevent`; this turns those into
# `attendance_dailyattendance`, which is what the register reads.
#
# **Order matters and is not enforced across the two boxes.** The plant agent
# runs at 12:45 and 23:15, this at 13:00 and 23:30. Run this first and it rolls
# up punches that have not arrived yet -- which is not an error, it is three
# hundred people quietly marked absent. That happened on 18 Sep 2026: the
# roll-up ran at 16:19, the punches landed at 16:25, and the whole workforce
# read ABSENT until it was re-run.
#
# The guard against that lives in the management command, not here: it refuses
# to run when the agent has not reported recently. This script therefore never
# passes --allow-stale. If it starts failing, the plant agent is the thing to
# look at -- the refusal is the system working.
#
#   ./rollup_attendance.sh          # the scheduled job: today and yesterday
#   ./rollup_attendance.sh 5        # widen the window to five days
#
# Exits non-zero on failure so cron reports it.

set -euo pipefail

APP="${FACTORY_APP_DIR:-/home/superadmin/django_projects/factory_app/current}"
PYTHON="$APP/.venv/bin/python"
LOG_DIR="${ATTENDANCE_LOG_DIR:-/home/superadmin/django_projects/logs}"
LOG="$LOG_DIR/attendance_rollup.log"
LOCK="/tmp/attendance_rollup.lock"

# Two days, not one. A late punch-out lands after midnight, so yesterday is not
# final until today has started -- and the 23:30 run would otherwise leave every
# night-shift worker on MISSING_PUNCH for good.
DAYS="${1:-2}"

mkdir -p "$LOG_DIR"

# Keep the log from growing without bound. One rotation is enough: this writes
# a handful of lines twice a day, so 5MB is already years of history.
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5242880 ]; then
    mv -f "$LOG" "$LOG.1"
fi

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

# Cron on this box mails nowhere, so a failure that only reached the log would
# be found by whoever went looking -- i.e. after payroll. The journal is the one
# place an alert can be hung off: `journalctl -t attendance-rollup -p err`.
fail() { say "$@"; logger -t attendance-rollup -p user.err "$*" || true; }

# A backfill run by hand and a scheduled run must not write the same rows at the
# same time. Skipping is right rather than queueing: the next run is 12 hours
# away and re-derives everything this one would have.
exec 9>"$LOCK"
if ! flock -n 9; then
    say "another roll-up is already running; skipping this one"
    exit 0
fi

if [ ! -x "$PYTHON" ]; then
    fail "FAILED: no interpreter at $PYTHON (has the release symlink moved?)"
    exit 1
fi

say "rolling up the last $DAYS day(s)"

# `cd` first: the default settings module reads .env from the project root, and
# on this server that is the live database. Run from anywhere else and Django
# falls back to defaults pointing at nothing.
cd "$APP"

if output="$("$PYTHON" manage.py sync_biometric_attendance --days "$DAYS" --quiet-progress 2>&1)"; then
    status=0
else
    status=$?
fi

while IFS= read -r line; do
    [ -n "$line" ] && say "  $line"
done <<< "$output"

if [ "$status" -eq 0 ]; then
    say "done"
    exit 0
fi

# Worth naming, because the two failures want different people. A stale mirror
# is the plant box or the LAN; anything else is this server.
if grep -q "Refusing to roll up" <<< "$output"; then
    fail "FAILED: the plant agent has not reported. Nothing was written -- the register"
    say "        keeps yesterday's rows rather than marking everybody absent."
    say "        Check attendance_sync.py on the plant box before re-running."
else
    fail "FAILED: the roll-up errored (exit $status). Nothing was committed."
fi
exit "$status"
