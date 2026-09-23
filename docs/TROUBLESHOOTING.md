# When it stops working

The failure this project exists around is a **silent** one: no punches arrive,
nothing errors, and three hundred people read as absent on a register payroll is
run from. So everything here is built to make the failure loud — a non-zero exit
code, a run row in the database, a refusal on the server side, and the logs this
page is about.

Nothing below needs a working attendance page or a browser. It can all be done
from the plant box.

---

## The three places to look, in this order

| | Where | Answers |
|---|---|---|
| 1 | `logs\task_wrapper.log` | Did the task fire at all, and with what exit code? |
| 2 | `logs\attendance_sync.log` | What did the script do, and why did it stop? |
| 3 | `attendance_sync.py --status` | Has it *ever* worked, and when did it stop? |

Two log files on purpose. The wrapper's log is the only evidence in the one case
the script cannot log anything — when Python never starts, because the venv moved
or the interpreter is gone. Without it, "the window closed and nothing happened"
is the entire account.

```powershell
cd C:\attendance-sync

# the last run, whatever it did
Get-Content logs\attendance_sync.log -Tail 40

# watch one live
.venv\Scripts\python.exe attendance_sync.py --days 1 --verbose

# every run this box has ever made, one line each
findstr RESULT logs\attendance_sync.log

# just the failures
findstr /C:"ERROR" logs\attendance_sync.log
```

---

## Reading one run

Every run writes the same three parts. Timestamps are plant time (IST), matching
the punches and every other clock in this system.

```
2026-09-19 12:45:01 IST  DEBUG   ========================================
2026-09-19 12:45:01 IST  DEBUG   attendance_sync starting
2026-09-19 12:45:01 IST  DEBUG     box      PLANT-PC01 as svc_attendance (pid 8124)
2026-09-19 12:45:01 IST  DEBUG     python   3.13.1 at C:\attendance-sync\.venv\Scripts\python.exe
2026-09-19 12:45:01 IST  DEBUG     cwd      C:\attendance-sync
2026-09-19 12:45:01 IST  DEBUG     env file C:\attendance-sync\.env
2026-09-19 12:45:01 IST  DEBUG     punch machine  10.x.x.x:1433 db=Biometrics table=punchtransfer user=readonly
2026-09-19 12:45:01 IST  DEBUG     factory db     <host>:5432 db=factory user=attendance_sync
2026-09-19 12:45:01 IST  DEBUG     passwords are never written here
```

**The preamble is the part that solves most problems**, because most problems
turn out to be "it was pointed at the wrong thing". Read it before the error:

- `cwd` not the repo → the task lost its working directory, and `.env` was never
  found.
- `env file ... <-- DOES NOT EXIST` → exactly that. It is stated outright.
- `table=` something other than `punchtransfer` → possibly an archive, which
  reads as everybody being absent.
- `python` not the venv's → the `.bat` fell back to the `PATH` interpreter, which
  may not have the drivers.
- Hosts not what you expect → `.env` was edited, by somebody, at some point.

Passwords are never written here, so this section is safe to paste into a ticket.

Then the progress, and one summary line per run:

```
2026-09-19 12:45:02 IST  INFO    Reading punches 2026-09-18 .. 2026-09-19
2026-09-19 12:45:14 IST  DEBUG   punchtransfer returned 1174 row(s) for 2026-09-18..2026-09-19 in 11.8s
2026-09-19 12:45:15 IST  DEBUG   3 of 1174 punch(es) were new (0.4s)
2026-09-19 12:45:15 IST  DEBUG   RESULT ok=1 window=2026-09-18..2026-09-19 pulled=1174 inserted=3 aliases=7 elapsed=14.2s
```

`findstr RESULT` over the file is then the agent's whole history on this box:
which run pulled nothing, which night it stopped, when it got slow.

**`inserted` much lower than `pulled` is healthy**, not a problem. The window is
two days wide, so yesterday's punches are already held and re-inserting them does
nothing. `pulled=0` is the one to look at.

---

## Symptom → cause

### The task says it worked, but no punches arrived

`Last Result: 0` and a green tick mean the `.bat` exited zero, which it only does
when the script did too. So look at what the run actually pulled:

```powershell
findstr RESULT logs\attendance_sync.log
```

`pulled=0` with the plant working is almost always `ATTENDANCE_PUNCH_TABLE`
pointed at an archive. Three of the four tables in that database stopped being
written to in 2025 and answer queries perfectly well. Confirm before changing it:

```sql
SELECT MAX(CombinedDatetime) FROM punchtransfer;
```

The script warns about this on its own — *"no punches at all between ..."* and,
under `--check`, *"the newest punch ... is <old date>"*.

### `FAILED: <SETTING> is not set. Copy .env.example to .env and fill it in.`

Either there is no `.env`, or the run did not start in the repo directory. The
preamble's `cwd` and `env file` lines say which. If it is the working directory,
the task is not going through `scheduling\run_attendance_sync.bat` — that wrapper
exists precisely to set it.

### `FAILED: Could not reach the punch database: ... TDS server is unavailable`

The SQL Server did not answer. Nothing to do with credentials yet.

```powershell
Test-NetConnection <punch-host> -Port 1433
```

False → the machine is off, moved address, or a firewall came up between it and
this box. True → the port is open but SQL Server is not listening on it, which is
a question for whoever administers the punch machines.

### `FAILED: Could not reach the punch database: ... Login failed for user`

Credentials. The login was disabled, its password rotated, or it lost rights to
`Biometrics`. Fix `.env`, then `--check`.

### `FAILED: Reading punches failed: ... Invalid object name 'punchtransfer'`

The table is not there under that name, or this login cannot see it. Check
`ATTENDANCE_PUNCH_TABLE`, then the login's grants.

### `FAILED: Could not reach the factory database: ... no pg_hba.conf entry for host`

Postgres is reachable and is refusing this host. Someone must add a `pg_hba.conf`
line for this box's address on the database server and reload. This is the
failure that follows a box move or a change of IP.

### `FAILED: Could not reach the factory database: ... password authentication failed`

`PG_USER` / `PG_PASSWORD`. If the role was recreated, the password changed with
it.

### `FAILED: ... permission denied for table attendance_punchevent`

The role exists but the grants did not survive — commonly after the table was
recreated by a migration, which drops its grants with it. Re-run the `GRANT`
block from [the setup guide](WINDOWS_SERVER_SETUP.md#5-create-the-postgres-role).

### `FAILED: The factory database does not match what this script writes`

The Django side moved: a column was renamed or a table dropped. The message names
each missing column.

**Do not work around this.** Writing into a half-matching schema is how bad data
gets in. The three tables are a contract; update `CONTRACT` in
`attendance_sync.py` to match `factory_app/attendance/models.py`, test it, and
ship both together.

### `warning: factory_codes read as empty`

The alias mirror was left alone rather than wiped — deliberately, because "could
not read it" and "there are none" look identical from here, and wiping it would
silently un-person the three people who punch under a `fac####` code and nothing
else.

One-off: ignore. Every run: chase it, and the reason is in the log file at DEBUG
level next to the warning.

### Everybody reads ABSENT on the register

Almost always ordering, not this agent. The roll-up on the application server
must run *after* the punches land — 13:00 and 23:30 against 12:45 and 23:15. Run
by hand in the wrong order and it scores a day whose punches have not arrived.

The server refuses to roll up a stale mirror rather than doing that, so:

```powershell
.venv\Scripts\python.exe attendance_sync.py --status
```

*"Past ATTENDANCE_SYNC_STALE_HOURS"* → this box is the problem, and the refusal
is the system working. *"The mirror is current"* → the problem is on the server,
not here.

### Punch times are five and a half hours out

Punches are stamped `+05:30` on the way in and the application converts back, so
this should not happen. If it does, something in that chain changed: check that
punches in `attendance_punchevent` carry an offset, not bare timestamps. Do not
"fix" it by shifting times here — that puts the correction in two places, and the
second one is always wrong first.

### `'python' is not recognized` in `task_wrapper.log`

The `.bat` fell through to the `PATH` interpreter and there is none for that
account. Usually the Microsoft Store build of Python, which is installed per-user
and invisible to a task running as somebody else. Reinstall from python.org for
all users, or recreate the venv.

### `FAILED: pymssql is not installed` / `psycopg2 is not installed`

The venv is gone, or a different interpreter ran. The preamble's `python` line
says which one. Recreate it:

```powershell
cd C:\attendance-sync
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### `FAILED, unexpectedly: <SomeError>`

A bug here rather than a problem out there. The full traceback is in
`logs\attendance_sync.log` immediately below that line. The run still exited
non-zero, so the server roll-up will refuse rather than mark anybody absent —
the punches are simply late, not wrong.

---

## Task Scheduler result codes

| Code | Means |
|---|---|
| `0x0` | Ran, and the script exited 0 |
| `0x1` | Ran, and the script **failed**. The reason is in `attendance_sync.log` |
| `0x2` | The `.bat` was not found. The task's path is wrong, or the repo moved |
| `0x41301` | Still running. Normal during a big backfill; suspicious for hours |
| `0x41303` | Has never run. The trigger never fired — check it is enabled |
| `0x8007010B` | Invalid working directory, from a "Start in" that was set by hand. Leave it empty; the `.bat` handles it |

A `0x1` with nothing in `attendance_sync.log` at that timestamp means Python
never started. That is what `task_wrapper.log` is for.

---

## Five-minute triage

```powershell
cd C:\attendance-sync

# 1. has it ever worked, and when did it stop?
.venv\Scripts\python.exe attendance_sync.py --status

# 2. are both ends reachable right now?
.venv\Scripts\python.exe attendance_sync.py --check

# 3. what did the last run actually do?
findstr RESULT logs\attendance_sync.log

# 4. did the task fire?
schtasks /Query /TN "Attendance Sync" /V /FO LIST | findstr /C:"Last Run" /C:"Last Result"
```

Once it is fixed, catch up whatever was missed. Re-running a range is always
safe — punches are unique on (code, timestamp, device), so a range already held
inserts nothing:

```powershell
.venv\Scripts\python.exe attendance_sync.py --date-from 2026-09-14 --date-to 2026-09-19
```

Then tell whoever runs the roll-up on the application server, so the register is
re-derived for those days. Copying the punches across does not re-score them.

---

## Escalating

Say which end is down, because it decides who fixes it:

| Evidence | Whose problem |
|---|---|
| `Could not reach the punch database`, `Invalid object name`, `Login failed` | The punch machines / plant IT |
| `Could not reach the factory database`, `no pg_hba.conf entry`, `permission denied` | The application server / database owner |
| `does not match what this script writes` | Whoever changed `factory_app/attendance/models.py` |
| `FAILED, unexpectedly` | This repo. It is a bug here |
| `--status` says stale but `--check` passes | The task is not firing. This box |

Attach the preamble block and the error from `logs\attendance_sync.log`. It
carries hosts, the table, the accounts and the timings, and **no passwords** —
that is why it is written the way it is.

Never attach `.env`.
