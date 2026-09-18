# attendance sync

Copies punches from the punching machines into the factory database.

Sibling of `factory_app/`, `FactoryFlow/` and `ji_mcp/`, and like them a
standalone project: run `git` inside this directory, never at the parent level.

## Why this exists

The punching machines write into a SQL Server box (`Biometrics`) on the factory
LAN. **The application server cannot reach it.** It used to: `factory_app`
queried the box directly on every sync, and when that stopped working the
attendance sheet stopped being filled at all.

So the job is split. This script runs on a Windows machine inside the plant that
can see both ends — the machines over the LAN, and Postgres over the network. It
reads punches and writes them into three tables. The Django application then
turns those into the attendance sheet exactly as it always did.

```
  This box (inside the plant)              Application server
  ─────────────────────────────            ──────────────────────────────
  MSSQL Biometrics                         attendance_punchevent
    punchtransfer     ──┐                  attendance_punchalias
    factory_codes     ──┴─ this script ──▶ attendance_punchsyncrun
                                                    │
                                                    ▼  manage.py
                                           attendance_dailyattendance
```

## What it deliberately does not do

**It resolves nothing.** It copies rows. Which employee a code belongs to, what
a day's punches amount to, who is on weekly off — all of that is business policy
and lives in `factory_app/attendance/`. If this script resolved aliases before
writing, fixing a wrong alias would only repair punches that arrived after the
fix; the ones already copied across would need a LAN link that does not exist.

**It never touches `attendance_dailyattendance`.** Nothing here decides whether
anybody was present.

## Setup

```bash
pip install -r requirements.txt
copy .env.example .env      # then fill it in
python attendance_sync.py --check
```

`--check` proves both ends work — SQL Server reachable, Postgres reachable, all
three tables present with the columns this script writes, and writes actually
permitted — without moving any data. Run it before scheduling anything. A
connection that silently failed looks identical to a quiet night, right up until
payroll.

### The Postgres role

Create a role that may only `SELECT` and `INSERT` (and `UPDATE`/`DELETE` on the
alias mirror) on the three `attendance_punch*` tables, and give it to `PG_USER`:

```sql
CREATE ROLE attendance_sync LOGIN PASSWORD '...';
GRANT SELECT, INSERT                 ON attendance_punchevent   TO attendance_sync;
GRANT SELECT, INSERT, UPDATE, DELETE ON attendance_punchalias   TO attendance_sync;
GRANT SELECT, INSERT                 ON attendance_punchsyncrun TO attendance_sync;
GRANT USAGE, SELECT ON SEQUENCE
    attendance_punchevent_id_seq,
    attendance_punchalias_id_seq,
    attendance_punchsyncrun_id_seq TO attendance_sync;
```

**Not the `postgres` superuser.** This box sits on the factory floor; that role
is the only thing between it and every other table in the business.

`pg_hba.conf` on the database server needs a line for this host, and the
firewall needs to let port 5432 through from it.

## Running it

```bash
python attendance_sync.py --days 2        # the nightly job
python attendance_sync.py --dry-run --days 2
python attendance_sync.py --date-from 2026-09-01 --date-to 2026-09-17   # backfill
```

`--days 2` is the default and the right nightly setting: a late punch-out lands
after midnight, so a day is not final until the next one has started.

Safe to re-run over any range. Punches carry a uniqueness constraint on
`(code, timestamp, device)`, so a repeated range inserts nothing.

### Scheduling

Task Scheduler, daily at 01:30:

| Field | Value |
|---|---|
| Program | `C:\path\to\python.exe` |
| Arguments | `attendance_sync.py --days 2` |
| Start in | `C:\path\to\sync` |

**Set "Start in"**, or the script will not find `.env`.

The application server's own roll-up should run *after* this, around 02:30:

```cron
30 2 * * *  cd /path/to/factory_app && ./venv/bin/python manage.py sync_biometric_attendance --days 2 --quiet-progress
```

## How you find out it failed

Every run writes a row to `attendance_punchsyncrun` — success or failure, with
the error text. Three places surface it:

- **Task Scheduler** shows a non-zero exit code. The script exits 1 on any failure.
- **The attendance page** shows an amber banner once the last successful run is
  older than `ATTENDANCE_SYNC_STALE_HOURS` (36 by default).
- **`manage.py sync_biometric_attendance` refuses to run** on a stale mirror and
  exits non-zero. This is the important one: rolling up punches that never
  arrived does not throw an error, it quietly marks three hundred people absent,
  and payroll is run from the result.

## Four things about the source schema

Missing any of these costs accuracy quietly, so they are worth repeating here.

1. **`paycode` is the JWPL employee code** — the same `JWPL0593` HR types into
   the hierarchy sheet. Not a machine id.
2. **There is no IN/OUT column.** A punch is a bare timestamp. First of the day
   is the arrival, last is the departure. About 14% of person-days carry a single
   punch, which the application scores `MISSING_PUNCH`.
3. **`status` means "transferred", not "attended".** It holds `New`/`Done`, the
   vendor ETL's own flag. This script does not read it.
4. **Only one of the four punch tables is live.** `punchtransfer` runs to today;
   the others stopped being written to in 2025. Pointing at an archive is silent —
   everybody reads as absent — which is why it is the `ATTENDANCE_PUNCH_TABLE`
   setting and not a literal.

`ipaddress` is a device serial (`NCD8244900570`), not an IP, whatever the column
is called.

### And one about time

SQL Server hands back naive datetimes meaning plant wall-clock time. Postgres
stores `timestamptz`. The script stamps every punch `+05:30` before writing —
left naive it would be read as UTC, every punch would shift by five and a half
hours, and the early shift would land on the previous day.

A fixed offset rather than `zoneinfo`: India has had no daylight saving since
1945, so it is exact, and it needs no IANA database — which Windows does not
ship, and whose absence would surface at 01:30 with nobody watching.

## The schema contract

This script writes Postgres directly, so it is coupled to these table and column
names. `--check` verifies all of them before any run, and refuses rather than
guessing if the Django side has moved:

| Table | Columns |
|---|---|
| `attendance_punchevent` | `raw_code`, `punched_at`, `device`, `ingested_at` |
| `attendance_punchalias` | `alias_code`, `employee_code`, `updated_at` |
| `attendance_punchsyncrun` | `started_at`, `finished_at`, `date_from`, `date_to`, `rows_pulled`, `rows_inserted`, `latest_punch_at`, `ok`, `detail` |

They are defined in `factory_app/attendance/models.py`; changing them there means
changing `CONTRACT` here in the same breath.
