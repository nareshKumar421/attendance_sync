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
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone

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
    try:
        return pymssql.connect(
            server=need("ATTENDANCE_DB_HOST"),
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
        raise SyncFailed(f"Could not reach the punch database: {exc}") from exc


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
    except Exception:
        return []

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
        raise SyncFailed(f"Reading punches failed: {exc}") from exc

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
    try:
        return psycopg2.connect(
            dbname=need("PG_NAME"),
            user=need("PG_USER"),
            password=need("PG_PASSWORD"),
            host=need("PG_HOST"),
            port=os.environ.get("PG_PORT", "5432"),
            connect_timeout=15,
            application_name="attendance_sync",
        )
    except SyncFailed:
        raise
    except Exception as exc:
        raise SyncFailed(f"Could not reach the factory database: {exc}") from exc


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
        raise SyncFailed(
            "The factory database does not match what this script writes:\n  "
            + "\n  ".join(missing)
            + "\nThe Django side has probably moved; do not force it."
        )


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
        cursor.execute("SELECT count(*) FROM attendance_punchalias")
        return cursor.fetchone()[0]


def write_punches(pg, punches):
    """Insert punches, skipping ones already held. Returns how many were new."""
    if not punches:
        return 0
    from psycopg2.extras import execute_values

    now = datetime.now(IST)
    inserted = 0
    with pg.cursor() as cursor:
        for start in range(0, len(punches), 5000):
            page = punches[start : start + 5000]
            returned = execute_values(
                cursor,
                "INSERT INTO attendance_punchevent "
                "(raw_code, punched_at, device, ingested_at) VALUES %s "
                "ON CONFLICT (raw_code, punched_at, device) DO NOTHING RETURNING 1",
                [(code, at, device, now) for code, at, device in page],
                fetch=True,
            )
            inserted += len(returned)
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


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def run_check():
    print("Checking the punch machines ...")
    mssql = mssql_connect()
    try:
        with mssql.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*), MAX(CombinedDatetime) FROM {punch_table()}")
            count, latest = cursor.fetchone()
        print(f"  ok: {punch_table()} holds {count} punches, newest {latest}")
        aliases = read_aliases(mssql)
        print(f"  ok: factory_codes maps {len(aliases)} alias(es)"
              if aliases else "  warning: factory_codes read as empty")
    finally:
        mssql.close()

    print("Checking the factory database ...")
    pg = pg_connect()
    try:
        check_contract(pg)
        print("  ok: all three tables present with the expected columns")
        # Prove the write actually works now, rather than at 01:30. Rolled back,
        # so nothing is left behind.
        with pg.cursor() as cursor:
            cursor.execute(
                "INSERT INTO attendance_punchalias (alias_code, employee_code, updated_at) "
                "VALUES ('__CHECK__', '__CHECK__', %s) ON CONFLICT (alias_code) DO NOTHING",
                (datetime.now(IST),),
            )
        pg.rollback()
        print("  ok: writes are permitted")
    finally:
        pg.close()

    print("\nBoth ends are reachable. Run with --dry-run next.")


def run_sync(date_from: date, date_to: date, dry_run: bool):
    started = datetime.now(IST)
    print(f"Reading punches {date_from} .. {date_to}")

    mssql = mssql_connect()
    try:
        aliases = read_aliases(mssql)
        punches = read_punches(mssql, date_from, date_to)
    finally:
        mssql.close()

    latest = max((at for _, at, _ in punches), default=None)
    print(f"  {len(punches)} punch(es), {len(aliases)} alias(es), newest {latest}")

    if dry_run:
        print("Dry run: nothing written.")
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
            write_run(pg, started=started, date_from=date_from, date_to=date_to,
                      pulled=len(punches), inserted=0, latest=latest, ok=False,
                      detail=f"Writing to the factory database failed: {exc}")
            pg.commit()
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

    print(f"  {inserted} new punch(es) stored"
          + (f", alias table now {alias_rows} row(s)" if alias_rows is not None else ""))
    print("Done. The app server rolls these up into the attendance sheet.")
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
    parser.add_argument("--dry-run", action="store_true",
                        help="Read and report, write nothing.")
    parser.add_argument("--env", default=".env", help="Path to the settings file.")
    args = parser.parse_args(argv)

    load_env(args.env)

    try:
        if args.check:
            run_check()
            return 0

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
        # which end was down, and a traceback buries that.
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
