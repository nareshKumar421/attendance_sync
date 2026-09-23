# Setting this up on a Windows Server

Start to finish, on a box that has never run it. Follow it in order; every step
ends in something you can see, so you never carry a broken assumption into the
next one.

Budget an hour, most of which is waiting for other people to open a firewall
port and create a database role.

> **The point of the box.** The punching machines write into a SQL Server
> (`Biometrics`) on the factory LAN, and the application server cannot reach it.
> This box can reach both, so it copies punches across. Nothing else about
> attendance is decided here — see [the README](../README.md) for what stays in
> the Django application and why.

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

---

## 0. Before you start

### The box

| | |
|---|---|
| OS | Windows Server 2016 or newer (Windows 10/11 Pro works too) |
| Where | **Inside the plant**, on the LAN segment that can see the punch machines |
| Stays on | Yes. It must be awake at 12:45 and 23:15 |
| Disk | A few hundred MB. The logs rotate and cap themselves |

Two BIOS/OS settings are worth doing now rather than after the first power cut:

- **Restore on AC power loss**, in the BIOS. The plant loses power; a box that
  stays off after it collects nothing and says nothing.
- **Never sleep**: `powercfg /change standby-timeout-ac 0`. A sleeping box misses
  its trigger. (The scheduled task is set to catch up on a missed run, but only
  once it is actually running again.)

**The clock.** Set the time zone to *(UTC+05:30) Chennai, Kolkata, Mumbai, New
Delhi* and leave Windows time sync on. Punch times do not depend on this clock —
they come off the machines and are stamped `+05:30` explicitly — but *when the
agent last ran* does, and the application refuses to roll up a mirror it thinks
is stale. A box an hour out is an argument nobody needs at payroll.

### The things to have in hand

Collect these before you touch the box. Every one of them blocks a later step.

| What | Where it comes from |
|---|---|
| Punch SQL Server host/IP, port, database | Whoever administers the punch machines |
| A SQL Server login and password with `SELECT` on it | Same |
| Which punch table is live | `punchtransfer` unless someone tells you otherwise — see [the README](../README.md#four-things-about-the-source-schema) |
| Factory Postgres host, port, database name | The application server's `.env` |
| A Postgres role for this agent, and its password | Created in step 5 — **not** the `postgres` superuser |
| An account to run the task as | Domain or local; it needs no admin rights |

### The network

Two outbound connections have to work **from this box**:

| To | Port | Fails as |
|---|---|---|
| Punch SQL Server | TCP 1433 | `Could not reach the punch database` |
| Factory Postgres | TCP 5432 | `Could not reach the factory database` |

Check them before installing anything, so a firewall does not masquerade as a
bad password later:

```powershell
Test-NetConnection <punch-host> -Port 1433
Test-NetConnection <postgres-host> -Port 5432
```

`TcpTestSucceeded : True` on both. If not, the fix is a firewall rule and a
`pg_hba.conf` line, not anything in this repo.

> The application server answers only at its **public** address. The LAN address
> does not respond, whatever the network diagram says — see the estate notes in
> `../../CLAUDE.md`.

---

## 1. Install Python

Download **Python 3.11 or newer, 64-bit**, from python.org and run the installer:

- ☑ **Add python.exe to PATH**
- ☑ **Install for all users** (puts it in `C:\Program Files\Python3xx`)

**Not the Microsoft Store build.** It installs into a per-user sandboxed
`AppData` path, and a scheduled task running as a *different* account cannot see
it. That failure arrives at 23:15 as "python is not recognized", in a log nobody
is reading.

```powershell
python --version
```

Anything from 3.11 up. 3.14 is fine — both dependencies ship wheels for it, which
is exactly what `requirements.txt` is pinned to guarantee.

---

## 2. Put the repo somewhere permanent

```powershell
cd C:\
git clone <repo-url> attendance-sync
```

No git on the box? Copy the folder over — it is four files and a `scheduling\`
directory. Either way it must end up at a **fixed path outside anyone's user
profile**:

```
C:\attendance-sync\
```

`C:\attendance-sync` is what `scheduling\AttendanceSync.xml` already points at,
so using it means one less thing to edit.

**Not the Desktop, not `Downloads`, not a profile folder.** Profile folders get
redirected, roamed and deleted when an account is disabled, and the scheduled
task then runs nothing at all — successfully, as far as Task Scheduler is
concerned.

---

## 3. Create the virtualenv

```powershell
cd C:\attendance-sync
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`run_attendance_sync.bat` looks for `.venv\Scripts\python.exe` first, then
`venv\`, then whatever `python` is on `PATH`. Either venv name works; a venv at
all is worth having, because it makes "the dependencies are missing" impossible
to confuse with "the wrong Python ran".

Confirm both drivers imported, which is the half of the install that can fail
quietly:

```powershell
.venv\Scripts\python.exe -c "import pymssql, psycopg2; print('drivers ok')"
```

<details>
<summary>If this box has no internet access</summary>

On a machine that does, with the same Python version and Windows architecture:

```powershell
pip download -r requirements.txt -d wheels --only-binary :all:
```

Copy the `wheels\` folder across, then:

```powershell
.venv\Scripts\python.exe -m pip install --no-index --find-links wheels -r requirements.txt
```

Both packages are published as wheels, so nothing needs a compiler. That is why
`requirements.txt` pins `psycopg2-binary` rather than `psycopg2`, and why the
pin was moved to 2.9.11 — 2.9.10's wheels stop at CPython 3.13, and on a 3.14
box pip silently falls back to building from source and fails for want of the
toolchain the binary package exists to avoid.
</details>

---

## 4. Fill in `.env`

```powershell
copy .env.example .env
notepad .env
```

Every setting, and where its value comes from:

| Setting | What to put |
|---|---|
| `ATTENDANCE_DB_HOST` | The punch SQL Server's IP or name, on the plant LAN |
| `ATTENDANCE_DB_PORT` | `1433` unless it was moved |
| `ATTENDANCE_DB_NAME` | `Biometrics` |
| `ATTENDANCE_DB_USER` / `_PASSWORD` | The SQL Server login. `SELECT` is all it needs |
| `ATTENDANCE_PUNCH_TABLE` | `punchtransfer`. **Change only with a `MAX(CombinedDatetime)` in hand** — three of the four tables in that database are archives, and pointing at one is silent: every employee reads as absent |
| `PG_HOST` / `PG_PORT` | The factory Postgres. Port `5432` |
| `PG_NAME` | The application's database, usually `factory` |
| `PG_USER` / `PG_PASSWORD` | The role from step 5 |

No quotes, no spaces around `=`. The file is read as flat `KEY=VALUE` lines and
anything else is ignored rather than rejected, so a stray quote becomes part of
the password.

**Then lock the file down.** It holds two sets of live database credentials on a
box that sits on the factory floor:

```powershell
icacls .env /inheritance:r /grant:r "Administrators:(R,W)" /grant:r "<task-account>:(R)"
```

`.env` is gitignored and must stay that way. It never gets committed, mailed or
pasted into a ticket.

---

## 5. Create the Postgres role

**On the application server**, not here. The role may touch the three
`attendance_punch*` tables and nothing else:

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

`UPDATE`/`DELETE` on the alias table only, because that one is a mirror and rows
that disappear upstream have to disappear here. The punch and run tables are
append-only by design.

**Do not give it the `postgres` superuser.** This box is physically accessible to
anyone on the shop floor, and that role is the only thing between it and every
other table in the business.

The database server also needs a `pg_hba.conf` line for this host and its
firewall open on 5432. Reload Postgres after editing `pg_hba.conf`.

---

## 6. Prove it works

Three commands, in this order. Each one goes further than the last, and stopping
at the first failure is the point.

### `--check` — both ends, no data moved

```powershell
cd C:\attendance-sync
.venv\Scripts\python.exe attendance_sync.py --check
```

```
Checking the punch machines ...
  ok: punchtransfer holds 412883 punches, newest 2026-09-19 11:42:07
  ok: factory_codes maps 7 alias(es)
Checking the factory database ...
  ok: all three tables present with the expected columns
  ok: writes are permitted

Both ends are reachable. Run with --dry-run next.
Log: C:\attendance-sync\logs\attendance_sync.log
```

It proves rather than assumes: the tables exist with the columns this script
writes, and a write is actually permitted — attempted for real and rolled back,
so nothing is left behind. A permission problem found here is a five-minute
`GRANT`; found at 23:15 it is a morning of everybody reading absent.

Two warnings it can give, both worth stopping for:

- *the newest punch is <old date>* — either the plant is shut, or
  `ATTENDANCE_PUNCH_TABLE` is pointed at an archive.
- *factory_codes read as empty* — the alias mirror will be left alone. A handful
  of people punch under a `fac####` code and three of them punch under nothing
  else, so if this is not a one-off, chase it before scheduling.

### `--dry-run` — read the real range, write nothing

```powershell
.venv\Scripts\python.exe attendance_sync.py --dry-run --days 2
```

A count of punches and the newest timestamp. If the count is zero and the plant
was working, stop here and read [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

### The first real run

Start narrow — one day, by hand, watching it:

```powershell
.venv\Scripts\python.exe attendance_sync.py --days 1
.venv\Scripts\python.exe attendance_sync.py --status
```

`--status` reads back what is now on record:

```
Last 1 run(s), newest first:
  2026-09-19 14:02  ok      2026-09-19..2026-09-19  pulled=1174 new=1174  1174 pulled, 1174 new, 7 aliases

1174 punch(es) stored, newest 2026-09-19 11:42:07+05:30
Last successful run 0.0 hour(s) ago. The mirror is current.
```

Then run the same command again. The second run must report **`new=0`**: the
range is already held, punches are unique on (code, timestamp, device), and
re-running changes nothing. That property is what makes every later backfill
safe, so it is worth seeing with your own eyes once.

Backfill the history you need before scheduling:

```powershell
.venv\Scripts\python.exe attendance_sync.py --date-from 2026-09-01 --date-to 2026-09-18
```

---

## 7. Schedule it

**Two boxes, twice a day, and the order between them is the whole thing.**

| | This box | Application server |
|---|---|---|
| Runs | **12:45 and 23:15** | 13:00 and 23:30 |
| What | `attendance_sync.py --days 2` | `manage.py sync_biometric_attendance --days 2` |
| Via | `scheduling\run_attendance_sync.bat` | `rollup_attendance.sh` in cron |

Fifteen minutes apart. The agent copies the punches; the server turns them into
the register. Roll up *before* the punches land and nothing errors — the register
simply marks the entire workforce absent, and payroll is run from it. That
happened on 18 Sep 2026: the roll-up ran at 16:19, the punches arrived at 16:25,
and 249 people read ABSENT until it was re-run.

Fifteen minutes is generous: 10,311 punches over 18 days pulled in under two
minutes.

`--days 2` on both sides, because the last punch-out of the night lands around
21:30 and a day scored before it is final strands every night-shift worker on
`MISSING_PUNCH`.

### Create the task

Import the supplied definition — one task, both triggers, with the retry and
catch-up settings already right:

```powershell
# Edit <Command> in the XML first if the repo is not at C:\attendance-sync
schtasks /Create /TN "Attendance Sync" /XML scheduling\AttendanceSync.xml /RU DOMAIN\account
```

Or create the two runs by hand, which needs no file editing:

```powershell
schtasks /Create /TN "Attendance Sync (midday)" /TR "C:\attendance-sync\scheduling\run_attendance_sync.bat" /SC DAILY /ST 12:45 /RU DOMAIN\account /RL LIMITED
schtasks /Create /TN "Attendance Sync (night)"  /TR "C:\attendance-sync\scheduling\run_attendance_sync.bat" /SC DAILY /ST 23:15 /RU DOMAIN\account /RL LIMITED
```

There is no "Start in" field to forget: the `.bat` sets its own working directory
from its own location. Without that, the script finds no `.env`, fails on the
first setting it reads, and looks exactly like a night nobody punched.

### The account it runs as

- **Run whether user is logged on or not.** Nobody is logged in at 23:15.
- It needs the **Log on as a batch job** right (`secpol.msc` → Local Policies →
  User Rights Assignment).
- It needs **write access to `C:\attendance-sync\logs`** — no logs, no diagnosis.
- It does **not** need to be an administrator. The supplied XML asks for
  `LeastPrivilege` deliberately.

The XML uses the **S4U** logon type, which means no password is stored with the
task and none has to be re-entered when the account's password rotates. The
trade-off: an S4U task gets no Windows *network* credentials. That is fine here,
because both databases are reached with their own credentials out of `.env` —
but if the punch database is ever switched to Windows authentication, this task
will start failing and this paragraph is the reason.

### Prove the task, not just the script

Running it by hand as yourself proves nothing about the account it will run as at
23:15. Force one:

```powershell
schtasks /Run /TN "Attendance Sync"
schtasks /Query /TN "Attendance Sync" /V /FO LIST | findstr /C:"Last Run" /C:"Last Result"
```

**`Last Result: 0`** — and `logs\task_wrapper.log` should have a new block
ending `OK: exit code 0`. Anything else, go to
[TROUBLESHOOTING.md](TROUBLESHOOTING.md); `0x1` and `0x2` mean quite different
things and both are listed there.

---

## 8. Hand-over checklist

Tick every line before you call it done.

- [ ] `--check` passes as the **task's account**, not just yours
- [ ] A real run has stored punches, and a second run over the same range added
      none
- [ ] `--status` shows a successful run and *"The mirror is current"*
- [ ] `schtasks /Run` finishes with `Last Result: 0`
- [ ] Both triggers exist: 12:45 and 23:15
- [ ] `logs\attendance_sync.log` and `logs\task_wrapper.log` both have content
- [ ] The application's attendance page shows today's punches, with no amber
      staleness banner
- [ ] The box does not sleep, and comes back on after a power cut
- [ ] `.env` is ACL'd, and is not in git
- [ ] Somebody other than you knows this box exists and what it does

---

## 9. Keeping it running

### What it writes

| File | What it is |
|---|---|
| `logs\attendance_sync.log` | The script's own: what it was pointed at, timings, the traceback on failure. Rotates at 2 MB, keeps 7 |
| `logs\task_wrapper.log` | The `.bat`'s: that the task fired, and its exit code. Rotates once at 1 MB |

Neither grows without bound, and neither needs pruning.

### The one-minute check

```powershell
cd C:\attendance-sync
.venv\Scripts\python.exe attendance_sync.py --status
```

*"The mirror is current"* and a recent `ok` row is the whole answer. Everything
else — the attendance page's amber banner, the roll-up's refusal — is downstream
of that one line.

### Routine

- **After any Windows Update reboot**, confirm the task still has both triggers
  and the box came back up. A missed run is caught up automatically once the box
  is running again, but only then.
- **When a password rotates** — the SQL Server login or the Postgres role — edit
  `.env` and run `--check` the same hour. Nothing else reads those credentials,
  so nothing else will tell you.
- **Never** change `ATTENDANCE_PUNCH_TABLE` without checking
  `MAX(CombinedDatetime)` on the table you are moving to.

### If the Django side changes

The three tables and their columns are a contract, listed in
[the README](../README.md#the-schema-contract) and enforced by `--check`. If
someone renames a column in `factory_app/attendance/models.py`, this agent
refuses rather than guessing — which is the correct outcome, and the fix is to
update `CONTRACT` in `attendance_sync.py` in the same change.

---

## 10. Moving or removing the box

**Moving it**: repeat steps 1–7 on the new machine, then delete the task on the
old one *before* enabling it on the new one. Two agents writing the same range is
harmless — the punches are unique — but two boxes both half-configured is how
you end up debugging the wrong one.

```powershell
schtasks /Delete /TN "Attendance Sync" /F
```

Also update the `pg_hba.conf` line and the firewall rule to the new address, and
tell whoever watches the application: the roll-up refusing is the *symptom* of a
plant box that has stopped reporting, and during a move that is expected rather
than alarming.

**Removing it entirely**: delete the task, then the folder. Nothing is installed
system-wide except Python itself, and the punches already copied across stay
where they are — they live in the application's database, not here.
