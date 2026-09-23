#!/usr/bin/env python3
"""
Copy punches from the punching machines into the factory database.

The machines write into a SQL Server box (``Biometrics``) on the factory LAN.
The application server cannot reach that box, so this script runs on a Windows
machine inside the plant that can see both: it reads punches over the LAN and
writes them into Postgres.

    python attendance_sync.py --check          # prove both ends work, move nothing
    python attendance_sync.py --days 2         # the nightly job
    python attendance_sync.py --date-from 2026-09-01 --date-to 2026-09-17

**This script resolves nothing.** It copies rows. Which employee a code belongs
to, what a day's punches amount to, who is on weekly off -- all of that is
business policy and stays in the Django application, where it can be re-applied
to punches copied across months ago. If this script resolved aliases before
writing, fixing a wrong alias would only repair punches arriving after the fix.

It writes exactly three tables and never touches ``attendance_dailyattendance``::

    attendance_punchevent     one raw punch, code exactly as punched
    attendance_punchalias     mirror of the machine's factory_codes table
    attendance_punchsyncrun   did this run happen, and did it work

Safe to re-run over any range: punches carry a uniqueness constraint on
(code, timestamp, device), so a repeated range inserts nothing.

Exits non-zero on any failure, so Task Scheduler shows a red result. A silent
failure here is the expensive one -- nobody's punches arrive, and everybody
reads as absent.

Every run also writes ``logs/attendance_sync.log`` next to this file: what it
was pointed at, how long each step took, and the full traceback on failure.
Task Scheduler keeps an exit code and nothing else, and the console window is
gone by the time anybody reads the result, so that file is usually the only
account of what happened at 23:15.

    python attendance_sync.py --status         # the last runs, read back out
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import socket
import sys
import time
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

# India has not observed daylight saving since 1945, so a fixed offset is exact
# and stays exact. Deliberately not zoneinfo: that needs the IANA database,
# which Windows does not ship, and a missing tzdata package would fail here at
# 01:30 with nobody watching.
IST = timezone(timedelta(hours=5, minutes=30))

PUNCH_TABLE_DEFAULT = "punchtransfer"

#: The schema this script writes. `--check` verifies every one of these exists,
#: because writing Postgres directly couples us to it: a column renamed on the
#: Django side would otherwise surface as a crash in the middle of the night.
CONTRACT = {
    "attendance_punchevent": ["raw_code", "punched_at", "device", "ingested_at"],
    "attendance_punchalias": ["alias_code", "employee_code", "updated_at"],
    "attendance_punchsyncrun": [
        "started_at", "finished_at", "date_from", "date_to",
        "rows_pulled", "rows_inserted", "latest_punch_at", "ok", "detail",
    ],
}


class SyncFailed(RuntimeError):
    """Something went wrong that must not look like 'nobody punched today'."""


# --------------------------------------------------------------------------
# The maintenance log
# --------------------------------------------------------------------------
#
# Everything the console prints goes to a file too, with a timestamp and the
# detail the console leaves out: what this run was pointed at, how long each
# step took, and the traceback behind a failure.
#
# This is not decoration. The run that matters is the one at 23:15 with nobody
# logged in, and it is read the next morning over RDP. Task Scheduler keeps an
# exit code; the console window is gone. Without a file, "it says failed" is
# the entire evidence, and the three failures that look identical from there --
# the LAN dropped, the credentials changed, the table was renamed -- each want
# a different person.

#: Next to this file, not %TEMP% or the working directory: whoever is debugging
#: this has the repo open already, and a log they cannot find is no log.
LOG_DIR_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_NAME = "attendance_sync.log"
LOG_BYTES = 2 * 1024 * 1024
LOG_KEEP = 7

log = logging.getLogger("attendance_sync")


def _ist_clock(seconds):
    """Log timestamps in plant time.

    Every other clock in this system is IST -- the punches, the run rows, the
    shift everybody works. A log in UTC or in whatever the box's locale happens
    to be forces mental arithmetic at exactly the moment nobody has the patience
    for it.
    """
    return datetime.fromtimestamp(seconds, IST).timetuple()


class _AtMost(logging.Filter):
    """Progress on stdout, trouble on stderr -- which is where they were."""

    def __init__(self, level):
        super().__init__()
        self.level = level

    def filter(self, record):
        return record.levelno <= self.level


def setup_logging(log_dir=LOG_DIR_DEFAULT, verbose=False):
    """Console exactly as before, plus a rotating file with everything.

    Returns the path being written, or ``None`` if no file could be opened.
    """
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False

    # No level prefix on the console: this output is read by people who are not
    # looking for a log, and "INFO: Done." reads worse than "Done."
    plain = logging.Formatter("%(message)s")

    to_stdout = logging.StreamHandler(sys.stdout)
    to_stdout.setLevel(logging.DEBUG if verbose else logging.INFO)
    to_stdout.addFilter(_AtMost(logging.INFO))
    to_stdout.setFormatter(plain)
    log.addHandler(to_stdout)

    to_stderr = logging.StreamHandler(sys.stderr)
    to_stderr.setLevel(logging.WARNING)
    to_stderr.setFormatter(plain)
    log.addHandler(to_stderr)

    # A Windows console is cp1252 by default, and a SQL Server error carrying a
    # smart quote would raise UnicodeEncodeError while reporting the real
    # failure -- losing it, and exiting on the wrong error.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    if not log_dir:
        return None

    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, LOG_NAME)
        # Rotating, because nobody prunes this box: twice a day for years still
        # fits in 2MB x 7, and a full disk in the plant would stop the punches
        # arriving -- the exact outcome this script exists to prevent.
        to_file = RotatingFileHandler(
            path, maxBytes=LOG_BYTES, backupCount=LOG_KEEP,
            encoding="utf-8", errors="replace",
        )
        to_file.setLevel(logging.DEBUG)
        file_format = logging.Formatter(
            "%(asctime)s  %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S IST"
        )
        file_format.converter = _ist_clock
        to_file.setFormatter(file_format)
        log.addHandler(to_file)
        return path
    except OSError as exc:
        # A log that cannot be opened is not a reason to skip the punches. Say
        # so loudly and carry on -- the run still writes attendance_punchsyncrun,
        # which is what the application actually reads.
        log.warning(f"Could not open a log file in {log_dir}: {exc}")
        log.warning("Continuing without one. Console output is the only record.")
        return None


def log_preamble(log_path, env_path):
    """Who ran this, with what, against what. File only -- it is for later.

    Nine failures in ten turn out to be "it was pointed at the wrong thing":
    the archive table, a host that moved subnet, an account whose password was
    rotated. None of that shows in the console output, and ``.env`` is edited
    by hand on a box several people have access to.
    """
    log.debug("=" * 72)
    log.debug("attendance_sync starting")
    try:
        who = getpass.getuser()
    except Exception:  # pragma: no cover - some service accounts have no name
        who = "unknown"
    log.debug(f"  box      {socket.gethostname()} as {who} (pid {os.getpid()})")
    log.debug(f"  python   {sys.version.split()[0]} at {sys.executable}")
    log.debug(f"  cwd      {os.getcwd()}")
    log.debug(f"  env file {os.path.abspath(env_path)}"
              + ("" if os.path.exists(env_path) else "  <-- DOES NOT EXIST"))
    log.debug(f"  log file {log_path or '(none -- console only)'}")
    log.debug(f"  argv     {' '.join(sys.argv[1:]) or '(no arguments)'}")
    # Masked deliberately: this file gets mailed around when something breaks.
    log.debug("  punch machine  %s:%s db=%s table=%s user=%s" % (
        os.environ.get("ATTENDANCE_DB_HOST", "(unset)"),
        os.environ.get("ATTENDANCE_DB_PORT", "1433"),
        os.environ.get("ATTENDANCE_DB_NAME", "(unset)"),
        os.environ.get("ATTENDANCE_PUNCH_TABLE", PUNCH_TABLE_DEFAULT),
        os.environ.get("ATTENDANCE_DB_USER", "(unset)"),
    ))
    log.debug("  factory db     %s:%s db=%s user=%s" % (
        os.environ.get("PG_HOST", "(unset)"),
        os.environ.get("PG_PORT", "5432"),
        os.environ.get("PG_NAME", "(unset)"),
        os.environ.get("PG_USER", "(unset)"),
    ))
    log.debug("  passwords are never written here")


def log_result(**fields):
    """One greppable line per run, whatever happened.

    A ``findstr RESULT`` over the log file is then the whole history of the
    agent on this box -- which run pulled nothing, which night it stopped.
    """
    log.debug("RESULT " + " ".join(f"{key}={value}" for key, value in fields.items()))


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def load_env(path=".env"):
    """Read KEY=VALUE from a file into os.environ, without overriding the shell.

    Deliberately not python-dotenv: two dependencies are easier to install on a
    locked-down Windows box than three, and this file is a flat key/value list.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def need(key):
    value = os.environ.get(key, "").strip()
    if not value:
        raise SyncFailed(f"{key} is not set. Copy .env.example to .env and fill it in.")
    return value


# --------------------------------------------------------------------------
# The punch machines (SQL Server)
# --------------------------------------------------------------------------


def mssql_connect():
    try:
        import pymssql
    except ImportError as exc:  # pragma: no cover - deployment problem, not logic
        raise SyncFailed(
            "pymssql is not installed. Run: pip install -r requirements.txt"
        ) from exc
    host = need("ATTENDANCE_DB_HOST")
    started = time.monotonic()
    try:
        connection = pymssql.connect(
            server=host,
            port=str(os.environ.get("ATTENDANCE_DB_PORT", "1433")),
            user=need("ATTENDANCE_DB_USER"),
            password=need("ATTENDANCE_DB_PASSWORD"),
            database=need("ATTENDANCE_DB_NAME"),
            login_timeout=15,
            timeout=120,
        )
    except SyncFailed:
        raise
    except Exception as exc:
        # Logged as well as raised: the caller turns this into one sentence on
        # stderr, and the driver's own wording underneath it is what tells you
        # whether the box refused the login or never answered at all.
        log.debug(f"pymssql.connect to {host} failed after "
                  f"{time.monotonic() - started:.1f}s")
        raise SyncFailed(f"Could not reach the punch database: {exc}") from exc
    log.debug(f"connected to the punch machines at {host} "
              f"in {time.monotonic() - started:.1f}s")
    return connection


def punch_table():
    """The live punch table.

    Three of the four tables in that database are archives that simply stopped
    being written to, and pointing at one is silent -- every employee reads as
    absent. Hence a setting with a deliberate default, checked as an identifier
    because it is interpolated into SQL.
    """
    name = os.environ.get("ATTENDANCE_PUNCH_TABLE", PUNCH_TABLE_DEFAULT).strip()
    if not name.replace("_", "").isalnum():
        raise SyncFailed(f"ATTENDANCE_PUNCH_TABLE {name!r} is not a table name.")
    return name


def read_aliases(connection):
    """``[(alias, real)]`` from the machine's ``factory_codes`` table.

    A handful of workers were enrolled under a ``fac####`` code instead of their
    JWPL one. Three of them punch under *nothing else*, so if this table is not
    mirrored they punch daily and read absent daily -- silently, which is the
    dangerous part.

    Returns an empty list rather than failing if the table is missing. The
    caller treats "empty" as "could not read" and leaves the mirror alone, so a
    vanished table never wipes a working mapping.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, empcode FROM factory_codes "
                "WHERE id IS NOT NULL AND empcode IS NOT NULL"
            )
            rows = cursor.fetchall()
    except Exception as exc:
        # Returning [] is deliberate (see above), but the reason must not be
        # thrown away with it: "read as empty" has to be separable from "the
        # table is gone" and from "this login cannot see it".
        log.debug(f"factory_codes could not be read: {exc}", exc_info=True)
        return []

    log.debug(f"factory_codes returned {len(rows)} row(s)")
    pairs = []
    for real, alias in rows:
        real = (real or "").strip().upper()
        alias = (alias or "").strip().upper()
        # A row pointing at itself is noise, not a mapping.
        if real and alias and real != alias:
            pairs.append((alias, real))
    return pairs


def read_punches(connection, date_from: date, date_to: date):
    """``[(raw_code, punched_at_ist, device)]`` for the range, inclusive.

    Everything is pulled: no code filter. The application knows who is on the
    payroll; this script does not, and a filter here would be a second place to
    get it wrong.
    """
    table = punch_table()
    started = time.monotonic()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT paycode, CombinedDatetime, ipaddress
                FROM {table}
                WHERE CombinedDatetime >= %s AND CombinedDatetime < DATEADD(day, 1, %s)
                  AND paycode IS NOT NULL AND CombinedDatetime IS NOT NULL
                ORDER BY paycode, CombinedDatetime
                """,
                (date_from, date_to),
            )
            rows = cursor.fetchall()
    except Exception as exc:
        log.debug(f"SELECT from {table} failed after "
                  f"{time.monotonic() - started:.1f}s")
        raise SyncFailed(f"Reading punches failed: {exc}") from exc

    # Worth having both numbers: rows that came back versus punches kept. A gap
    # between them means codes or timestamps are arriving null, which is a
    # machine problem and not visible anywhere else.
    log.debug(f"{table} returned {len(rows)} row(s) for {date_from}..{date_to} "
              f"in {time.monotonic() - started:.1f}s")
    punches = []
    for paycode, punched_at, device in rows:
        code = (paycode or "").strip().upper()
        if not code or punched_at is None:
            continue
        # SQL Server hands back a naive datetime that means plant wall-clock
        # time. Postgres stores timestamptz, so the offset has to be stated --
        # left naive it would be read as UTC and every punch would shift by 5h30,
        # putting the early shift on the previous day.
        punches.append((code, punched_at.replace(tzinfo=IST), (device or "").strip()))
    if len(punches) != len(rows):
        log.debug(f"{len(rows) - len(punches)} row(s) skipped: no code or no timestamp")
    return punches


# --------------------------------------------------------------------------
# The factory database (Postgres)
# --------------------------------------------------------------------------


def pg_connect():
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover - deployment problem
        raise SyncFailed(
            "psycopg2 is not installed. Run: pip install -r requirements.txt"
        ) from exc
    host = need("PG_HOST")
    started = time.monotonic()
    try:
        connection = psycopg2.connect(
            dbname=need("PG_NAME"),
            user=need("PG_USER"),
            password=need("PG_PASSWORD"),
            host=host,
            port=os.environ.get("PG_PORT", "5432"),
            connect_timeout=15,
            # Names this agent in pg_stat_activity, so a connection from the
            # plant box is identifiable from the server side without guessing.
            application_name="attendance_sync",
        )
    except SyncFailed:
        raise
    except Exception as exc:
        log.debug(f"psycopg2.connect to {host} failed after "
                  f"{time.monotonic() - started:.1f}s")
        raise SyncFailed(f"Could not reach the factory database: {exc}") from exc
    log.debug(f"connected to the factory database at {host} "
              f"in {time.monotonic() - started:.1f}s")
    return connection


def check_contract(pg):
    """Every table and column this script writes must exist, with that name."""
    missing = []
    with pg.cursor() as cursor:
        for table, columns in CONTRACT.items():
            cursor.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,),
            )
            present = {row[0] for row in cursor.fetchall()}
            if not present:
                missing.append(f"table {table} does not exist")
                continue
            for column in columns:
                if column not in present:
                    missing.append(f"{table}.{column} does not exist")
    if missing:
        for problem in missing:
            log.debug(f"contract broken: {problem}")
        raise SyncFailed(
            "The factory database does not match what this script writes:\n  "
            + "\n  ".join(missing)
            + "\nThe Django side has probably moved; do not force it."
        )
    log.debug(f"contract ok: {', '.join(CONTRACT)}")


def write_aliases(pg, pairs):
    """Mirror the alias table. Returns how many rows it now holds."""
    if not pairs:
        # Empty means "could not read it" as often as "there are none", and
        # wiping a working mapping would silently un-person three people.
        return None
    from psycopg2.extras import execute_values

    now = datetime.now(IST)
    with pg.cursor() as cursor:
        execute_values(
            cursor,
            "INSERT INTO attendance_punchalias (alias_code, employee_code, updated_at) "
            "VALUES %s ON CONFLICT (alias_code) DO UPDATE SET "
            "employee_code = EXCLUDED.employee_code, updated_at = EXCLUDED.updated_at",
            [(alias, real, now) for alias, real in pairs],
        )
        # An alias deleted upstream must go, or punches keep being routed to
        # somebody who no longer owns that code. Only ever runs when the read
        # actually returned rows.
        cursor.execute(
            "DELETE FROM attendance_punchalias WHERE alias_code <> ALL(%s)",
            ([alias for alias, _ in pairs],),
        )
        log.debug(f"{cursor.rowcount} alias(es) deleted as no longer upstream")
        cursor.execute("SELECT count(*) FROM attendance_punchalias")
        return cursor.fetchone()[0]


def write_punches(pg, punches):
    """Insert punches, skipping ones already held. Returns how many were new."""
    if not punches:
        return 0
    from psycopg2.extras import execute_values

    now = datetime.now(IST)
    inserted = 0
    started = time.monotonic()
    with pg.cursor() as cursor:
        for start in range(0, len(punches), 5000):
            page = punches[start : start + 5000]
            log.debug(f"inserting punches {start + 1}..{start + len(page)} "
                      f"of {len(punches)}")
            returned = execute_values(
                cursor,
                "INSERT INTO attendance_punchevent "
                "(raw_code, punched_at, device, ingested_at) VALUES %s "
                "ON CONFLICT (raw_code, punched_at, device) DO NOTHING RETURNING 1",
                [(code, at, device, now) for code, at, device in page],
                fetch=True,
            )
            inserted += len(returned)
    # inserted < pulled is the normal, healthy case on a re-run: the window is
    # two days wide and yesterday's punches are already held.
    log.debug(f"{inserted} of {len(punches)} punch(es) were new "
              f"({time.monotonic() - started:.1f}s)")
    return inserted


def write_run(pg, *, started, date_from, date_to, pulled, inserted, latest, ok, detail):
    """Record that this run happened. The application reads the newest row.

    Written whether the run worked or not: a failed run and a run that never
    happened have to be distinguishable from each other, and both from a day on
    which nobody punched.
    """
    with pg.cursor() as cursor:
        cursor.execute(
            "INSERT INTO attendance_punchsyncrun "
            "(started_at, finished_at, date_from, date_to, rows_pulled, rows_inserted, "
            " latest_punch_at, ok, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (started, datetime.now(IST), date_from, date_to, pulled, inserted,
             latest, ok, detail[:2000]),
        )
    log.debug(f"attendance_punchsyncrun row written: ok={ok}, {detail[:200]}")


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def run_check():
    log.info("Checking the punch machines ...")
    mssql = mssql_connect()
    try:
        with mssql.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*), MAX(CombinedDatetime) FROM {punch_table()}")
            count, latest = cursor.fetchone()
        log.info(f"  ok: {punch_table()} holds {count} punches, newest {latest}")
        # A live table is one being written to now. An archive answers this
        # query perfectly well and stopped taking punches in 2025.
        if latest is not None and (datetime.now(IST).date() - latest.date()).days > 2:
            log.warning(f"  warning: the newest punch in {punch_table()} is "
                        f"{latest}. Either the plant is shut, or this is an "
                        f"archive table and ATTENDANCE_PUNCH_TABLE is wrong.")
        aliases = read_aliases(mssql)
        if aliases:
            log.info(f"  ok: factory_codes maps {len(aliases)} alias(es)")
        else:
            log.warning("  warning: factory_codes read as empty")
    finally:
        mssql.close()

    log.info("Checking the factory database ...")
    pg = pg_connect()
    try:
        check_contract(pg)
        log.info("  ok: all three tables present with the expected columns")
        # Prove the write actually works now, rather than at 01:30. Rolled back,
        # so nothing is left behind.
        with pg.cursor() as cursor:
            cursor.execute(
                "INSERT INTO attendance_punchalias (alias_code, employee_code, updated_at) "
                "VALUES ('__CHECK__', '__CHECK__', %s) ON CONFLICT (alias_code) DO NOTHING",
                (datetime.now(IST),),
            )
        pg.rollback()
        log.info("  ok: writes are permitted")
    finally:
        pg.close()

    log.info("\nBoth ends are reachable. Run with --dry-run next.")


def run_status(limit=10):
    """The agent's own history, read back out of the factory database.

    This box has no psql and often no browser, so without this there is no way
    to answer "has this ever worked, and when did it stop?" from the machine
    you are standing at -- which is the machine the answer is about.

    Reads only. Safe to run while a sync is in progress.
    """
    pg = pg_connect()
    try:
        with pg.cursor() as cursor:
            cursor.execute(
                "SELECT started_at, date_from, date_to, rows_pulled, rows_inserted, "
                "ok, detail FROM attendance_punchsyncrun "
                "ORDER BY started_at DESC LIMIT %s",
                (limit,),
            )
            runs = cursor.fetchall()
            cursor.execute(
                "SELECT count(*), max(punched_at) FROM attendance_punchevent"
            )
            stored, newest = cursor.fetchone()
            cursor.execute(
                "SELECT max(started_at) FROM attendance_punchsyncrun WHERE ok"
            )
            last_good = cursor.fetchone()[0]
    finally:
        pg.close()

    if not runs:
        log.warning("No runs recorded at all -- this agent has never written here.")
        log.warning("Check PG_NAME: an empty history usually means the right")
        log.warning("credentials against the wrong database.")
        return 0

    log.info(f"Last {len(runs)} run(s), newest first:")
    for started, d_from, d_to, pulled, inserted, ok, detail in runs:
        # First line only: a failure detail carries the driver's whole multi-line
        # complaint, and this is a table. `detail` is blank on some rows, so the
        # empty case has to survive rather than take the diagnosis down with it.
        first_line = next(iter((detail or "").splitlines()), "")
        log.info(f"  {started.astimezone(IST):%Y-%m-%d %H:%M}  "
                 f"{'ok    ' if ok else 'FAILED'}  {d_from}..{d_to}  "
                 f"pulled={pulled} new={inserted}  {first_line[:70]}")

    log.info(f"\n{stored} punch(es) stored, newest {newest.astimezone(IST) if newest else 'none'}")

    # The number that decides whether the roll-up will run at all. The server
    # refuses on a stale mirror rather than marking the workforce absent, so
    # this is the first thing to look at when the register stops updating.
    if last_good is None:
        log.warning("No successful run on record. The roll-up will refuse.")
    else:
        age = (datetime.now(IST) - last_good.astimezone(IST)).total_seconds() / 3600
        line = f"Last successful run {age:.1f} hour(s) ago."
        if age > 36:
            log.warning(line + " Past ATTENDANCE_SYNC_STALE_HOURS (36 by")
            log.warning("default), so the server roll-up is refusing to run and the")
            log.warning("attendance page is showing its amber banner.")
        else:
            log.info(line + " The mirror is current.")
    return 0


def run_sync(date_from: date, date_to: date, dry_run: bool):
    started = datetime.now(IST)
    clock = time.monotonic()
    log.info(f"Reading punches {date_from} .. {date_to}")

    mssql = mssql_connect()
    try:
        aliases = read_aliases(mssql)
        punches = read_punches(mssql, date_from, date_to)
    finally:
        mssql.close()

    latest = max((at for _, at, _ in punches), default=None)
    log.info(f"  {len(punches)} punch(es), {len(aliases)} alias(es), newest {latest}")

    # Not an error -- a shut plant looks like this too -- but it is the shape of
    # every silent failure this project exists around, so it is never implicit.
    if not punches:
        log.warning(f"  warning: no punches at all between {date_from} and "
                    f"{date_to}. If the plant was working, check "
                    f"ATTENDANCE_PUNCH_TABLE and the machines themselves.")

    if dry_run:
        log.info("Dry run: nothing written.")
        log_result(ok="dry-run", window=f"{date_from}..{date_to}",
                   pulled=len(punches), aliases=len(aliases),
                   elapsed=f"{time.monotonic() - clock:.1f}s")
        return 0

    pg = pg_connect()
    try:
        check_contract(pg)
        try:
            alias_rows = write_aliases(pg, aliases)
            inserted = write_punches(pg, punches)
            pg.commit()
        except Exception as exc:
            pg.rollback()
            # The run row is the only way the application learns this failed, so
            # it is written on its own connection state, outside the rollback.
            log.debug("the write transaction was rolled back")
            write_run(pg, started=started, date_from=date_from, date_to=date_to,
                      pulled=len(punches), inserted=0, latest=latest, ok=False,
                      detail=f"Writing to the factory database failed: {exc}")
            pg.commit()
            log_result(ok=0, window=f"{date_from}..{date_to}", pulled=len(punches),
                       inserted=0, elapsed=f"{time.monotonic() - clock:.1f}s",
                       detail=repr(str(exc)[:200]))
            raise SyncFailed(f"Writing to the factory database failed: {exc}") from exc

        detail = (f"{len(punches)} pulled, {inserted} new"
                  + (f", {alias_rows} aliases" if alias_rows is not None else
                     ", alias table left alone (read as empty)"))
        write_run(pg, started=started, date_from=date_from, date_to=date_to,
                  pulled=len(punches), inserted=inserted, latest=latest, ok=True,
                  detail=detail)
        pg.commit()
    finally:
        pg.close()

    log.info(f"  {inserted} new punch(es) stored"
             + (f", alias table now {alias_rows} row(s)" if alias_rows is not None else ""))
    log.info("Done. The app server rolls these up into the attendance sheet.")
    log_result(ok=1, window=f"{date_from}..{date_to}", pulled=len(punches),
               inserted=inserted, aliases=alias_rows,
               latest=latest.isoformat() if latest else None,
               elapsed=f"{time.monotonic() - clock:.1f}s")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Copy punches from the punching machines into the factory database."
    )
    parser.add_argument("--date-from", help="YYYY-MM-DD. Defaults to --days back from --date-to.")
    parser.add_argument("--date-to", help="YYYY-MM-DD. Defaults to today.")
    parser.add_argument(
        "--days", type=int, default=2,
        help="How many days back to cover when --date-from is not given. Default 2: a late "
             "punch-out lands after midnight, so a day is not final until the next has started.",
    )
    parser.add_argument("--check", action="store_true",
                        help="Prove both ends work and stop. Moves no data.")
    parser.add_argument("--status", action="store_true",
                        help="Show the runs already recorded and stop. Reads only.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Read and report, write nothing.")
    parser.add_argument("--env", default=".env", help="Path to the settings file.")
    parser.add_argument("--log-dir", default=LOG_DIR_DEFAULT,
                        help="Where attendance_sync.log is written. Empty to write none.")
    parser.add_argument("--verbose", action="store_true",
                        help="Put the log's detail on the console too. The file "
                             "always has it, so this is for watching a run live.")
    args = parser.parse_args(argv)

    # Before anything that can fail, so that whatever happens next is recorded.
    log_path = setup_logging(args.log_dir, args.verbose)
    load_env(args.env)
    log_preamble(log_path, args.env)

    try:
        if args.check:
            run_check()
            if log_path:
                log.info(f"Log: {log_path}")
            return 0

        if args.status:
            return run_status()

        today = datetime.now(IST).date()
        date_to = (datetime.strptime(args.date_to, "%Y-%m-%d").date()
                   if args.date_to else today)
        date_from = (datetime.strptime(args.date_from, "%Y-%m-%d").date()
                     if args.date_from else date_to - timedelta(days=max(args.days, 1) - 1))
        if date_from > date_to:
            raise SyncFailed("--date-from is after --date-to.")
        return run_sync(date_from, date_to, args.dry_run)
    except SyncFailed as exc:
        # The sentence, not the frames: whoever reads this at 08:00 needs to know
        # which end was down, and a traceback buries that. The frames go to the
        # log file, where they are there for the case the sentence is not enough.
        log.error(f"FAILED: {exc}")
        log.debug("the failure above, in full:", exc_info=True)
        log_result(ok=0, detail=repr(str(exc).splitlines()[0][:200]))
        if log_path:
            log.error(f"Details: {log_path}")
        return 1
    except KeyboardInterrupt:
        log.error("Interrupted.")
        log_result(ok=0, detail="interrupted by hand")
        return 130
    except Exception as exc:  # pragma: no cover - the unanticipated one
        # A crash that is not a SyncFailed is a bug here rather than a problem
        # out there, and it is the one failure with no written account at all:
        # the traceback goes to a console that closed hours ago. Catch it, file
        # it, and still exit non-zero so the roll-up refuses rather than marking
        # the workforce absent.
        log.error(f"FAILED, unexpectedly: {exc.__class__.__name__}: {exc}")
        log.debug("the crash above, in full:", exc_info=True)
        log_result(ok=0, detail=repr(f"{exc.__class__.__name__}: {exc}"[:200]))
        if log_path:
            log.error(f"Traceback: {log_path}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
