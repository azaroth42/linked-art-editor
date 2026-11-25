# Native installation and operation

Everything runs natively on the host; there is no container setup. Three kinds of process make up
an installation:

| Process | Development | Production |
| :-- | :-- | :-- |
| PostgreSQL 16+ with `vector`, `pg_trgm`, `btree_gin` | an already-running server (Postgres.app, Homebrew, or the distribution package); `make up` creates the database and extensions but never starts or stops the server | the distribution's `postgresql` service |
| QLever (`qlever-server`) | `make up` / `make down`, through qlever-control and `deploy/Qleverfile` | `systemd/qlever.service` |
| `lae` processes: `api`, `crdt-ws`, `sync-worker`, `scheduler` | `make run` (honcho with `deploy/Procfile`) | `systemd/lae-<name>.service` |

Later phases add `embedder`, `reconcile`, and `llm` (commented out in `Procfile` until their
`lae` sub-commands exist). `lae api` serves the HTTP API on `LAE_API_HOST:LAE_API_PORT` (default
`127.0.0.1:8000`). `crdt-ws`, `sync-worker`, and `scheduler` are built in Phase 1 Steps 1.8, 1.11,
and 1.15; until then each prints the step that builds it and exits with status 3, so honcho stops
`make run` at once. Run `uv run --project backend lae api` on its own in the meantime.

`lae scheduler` runs periodic jobs (Phase 1: the hourly draft reaper, which discards drafts idle
for `LAE_DRAFT_TTL_DAYS`, default 5, through `crdt-ws` at `LAE_CRDT_WS_URL` with
`LAE_INTERNAL_TOKEN`). Each job takes a PostgreSQL advisory lock while it runs, so a second
scheduler never runs the same job at once; each records its last run in `sync_state` under
`job:<name>` (`last_run`, `last_status`, `duration`, `last_error`, `result`, `next_run`). On
`SIGTERM` it lets running jobs finish their current item (up to 30 s) and exits 0.

Every `lae` process writes JSON log lines to stdout (journald in production), with `request_id`
on every line of an HTTP request; `LAE_LOG_LEVEL` sets the level (default `info`). Probes:
`GET /health/live` is always 200; `GET /health/ready` is 200 when PostgreSQL and (if
`LAE_QLEVER_URL` is set) QLever answer, else 503 with the failing components in `failing`.

## Files

| Path | Purpose |
| :-- | :-- |
| `Qleverfile` | qlever-control configuration: index `lae`, `TEXT_INDEX = from_literals`, `ADD_HAS_WORD_TRIPLES = true`, `PERSIST_UPDATES = true`, `ENABLE_METRICS = true` (Prometheus `/metrics`, token required), `SYSTEM = native`, laptop-sized memory. Reads `QLEVER_PORT`, `QLEVER_INPUT_FILES`, `LAE_QLEVER_ACCESS_TOKEN`. |
| `postgres/init.sql` | Creates the `vector`, `pg_trgm`, and `btree_gin` extensions (idempotent). |
| `Procfile` | Process list for `make run` (honcho). |
| `systemd/` | Unit templates: `lae-api`, `lae-crdt-ws`, `lae-sync-worker`, `lae-scheduler`, `qlever`. |
| `QLEVER_VERSION` | The QLever binary this repository was verified with, and the reference source commit. |
| `data/` | Default development index directory (`QLEVER_DIR`); not committed. |

## Install on macOS (Apple Silicon)

**PostgreSQL**, either of:

- [Postgres.app](https://postgresapp.com/) (PostgreSQL 18; `vector`, `pg_trgm`, and `btree_gin` are
  bundled). Start it from the app, then put its client tools on `PATH` (for example in
  `~/.zprofile`):

  ```sh
  export PATH="/Applications/Postgres.app/Contents/Versions/latest/bin:$PATH"
  ```

  The Makefile finds these tools by itself when Postgres.app is installed.
- Homebrew (the `pgvector` formula is built for `postgresql@17` and `postgresql@18`):

  ```sh
  brew install postgresql@18 pgvector
  brew services start postgresql@18
  export PATH="$(brew --prefix postgresql@18)/bin:$PATH"   # postgresql@18 is keg-only
  ```

**QLever** from the `qlever-dev/qlever` tap. The `qlever` formula installs `qlever-index`,
`qlever-server`, and qlever-control's `qlever` CLI (formula `qlever-control`):

```sh
brew tap qlever-dev/qlever
brew install qlever
qlever-server --version   # compare with deploy/QLEVER_VERSION
```

## Install on Debian and Ubuntu

**PostgreSQL 16 with pgvector** from the PostgreSQL project's apt repository (PGDG). `pg_trgm` and
`btree_gin` ship with the server package.

```sh
sudo apt install -y postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
sudo apt install -y postgresql-16 postgresql-16-pgvector
```

For development, give your login a superuser role so that `make up` can create the database and
the extensions: `sudo -u postgres createuser --superuser "$USER"`.

**QLever** from its native packages (amd64; see [docs.qlever.dev](https://docs.qlever.dev/quickstart/)):

```sh
sudo apt update && sudo apt install -y wget gpg ca-certificates
wget -qO - https://packages.qlever.dev/pub.asc | gpg --dearmor | sudo tee /usr/share/keyrings/qlever.gpg > /dev/null
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/qlever.gpg] https://packages.qlever.dev/ $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") main" | sudo tee /etc/apt/sources.list.d/qlever.list
sudo apt update && sudo apt install -y qlever
qlever-server --version   # compare with deploy/QLEVER_VERSION
```

If the `qlever` CLI is not on `PATH` afterwards, install qlever-control with `pipx install qlever`.
On other architectures, build `qlever-index` and `qlever-server` from source
([ad-freiburg/qlever](https://github.com/ad-freiburg/qlever), CMake), put them on `PATH`, and install
qlever-control with `pipx install qlever`. Record the commit you built in `QLEVER_VERSION`.

## Development

Prerequisites: PostgreSQL running, QLever installed, `make setup` done (see the root README).

```sh
make up     # create the database + extensions if missing; build an empty QLever index if missing; start QLever
make run    # run the lae processes from deploy/Procfile (Ctrl-C stops them)
make logs   # follow the QLever server log
make down   # stop QLever (PostgreSQL and the database stay)
```

`make up` is idempotent and prints the `LAE_*` settings for the `lae` processes. Put them in a
`.env` file in the repository root (not committed); honcho loads it for `make run`.

`qlever-server` listens on all interfaces (QLever has no bind option). On an untrusted network,
keep the macOS firewall on or block the QLever port.

Every port, database name, and directory can be overridden from the environment or on the `make`
command line:

| Variable | Default | Used for |
| :-- | :-- | :-- |
| `PGHOST` | unset (default Unix socket) | PostgreSQL host or socket directory (read by `psql`, `createdb`, `pg_isready`) |
| `PGPORT` | `5432` | PostgreSQL port |
| `PGUSER` | unset (your login) | PostgreSQL role |
| `LAE_DB` | `lae` | database created by `make up` |
| `QLEVER_PORT` | `7010` | QLever port; `make down` stops only the server on this port |
| `QLEVER_DIR` | `deploy/data/qlever` | QLever index directory |
| `QLEVER_INPUT_FILES` | `*.nt` | N-Quads or N-Triples (plain or gzipped) for a new index, relative to `QLEVER_DIR` |
| `LAE_QLEVER_ACCESS_TOKEN` | unset | token for SPARQL Update; without it QLever is read-only |
| `PG_BIN` | Postgres.app's `bin` if installed | directory of `psql`, `createdb`, `pg_isready` |
| `QLEVER` | `qlever` | qlever-control executable |

Several stacks can share one machine, for example a second one on its own database and port:

```sh
make up   LAE_DB=lae_i3 QLEVER_PORT=7003 QLEVER_DIR=/tmp/lae-i3/qlever
make down QLEVER_PORT=7003
```

The index starts empty: `qlever get-data` only creates an empty `lae-empty.nt`. To rebuild from
other N-Triples, stop QLever, empty the index directory, and run `make up` with
`QLEVER_INPUT_FILES` pointing at the files (plain or gzipped; relative to the index
directory). From Phase 1 on, `lae bootstrap` builds the index from Postgres:

```sh
cd backend
uv run lae bootstrap aat-labels --csv <aat_dataset.csv>    # AAT labels (Phase 1 sidecar stand-in)
uv run lae bootstrap load --input <dir-or-jsonl>           # records, labels, aliases, findings (COPY)
uv run lae bootstrap export-nt --out /data/export.nt.gz     # records + sidecar labels as N-Quads
uv run lae bootstrap build-index --nt /data/export.nt.gz --dir <fresh index dir>
```

`build-index` builds in a fresh directory, starts the server with `--persist-updates` on the
port of `LAE_QLEVER_URL` (with `LAE_QLEVER_ACCESS_TOKEN` for updates), records the watermark in
`sync_state`, and prints the command that stops exactly that server. Each command prints a JSON
summary; records that were not loaded are listed in a JSON Lines report. Timings:
`docs/spikes/S6-bootstrap.md`.

`load` gives every record that arrives with a URI from elsewhere a new `<LAE_BASE_URI><uuid>` URI
and keeps the old one in `record_aliases` (ADR-0030), so set `LAE_BASE_URI` before the first
load. Labels loaded before the records let `load` strip the `_label` of references to them. A
record that fails JSON Schema is loaded and listed in `qa_findings`; one that cannot be written
to RDF is rejected. Loading the same input again writes nothing. After an interrupted `load`,
or after loading more labels, run `uv run lae bootstrap rewrite-refs`; after any `load` or
`rewrite-refs` on a database that already has an index, run `export-nt` and `build-index` again.
Old URIs on the editor's own host are redirected by the `api` process: route requests that match
nothing else to it. Old URIs on another host must be redirected there
(`SELECT alias_uri, record_id FROM record_aliases` gives the map).

To run qlever-control by hand, work in the index directory and pass the Qleverfile:

```sh
cd deploy/data/qlever
qlever --qleverfile ../../Qleverfile status
```

## Production with systemd

The unit templates in `systemd/` assume this layout; adjust paths and the user if yours differs.

| Path | Contents |
| :-- | :-- |
| `/opt/lae` | a checkout of this repository |
| `/opt/lae/backend/.venv` | the backend environment (`uv sync --locked --no-dev`) |
| `/var/lib/lae/qlever` | the QLever index directory |
| `/etc/lae/lae.env` | `LAE_*` settings and `QLEVER_PORT`, readable by the `lae` group only |
| user and group `lae` | runs every unit |

1. Install PostgreSQL and QLever as above, plus [`uv`](https://docs.astral.sh/uv/).
2. Create the user, directories, and backend environment:

   ```sh
   sudo useradd --system --home-dir /var/lib/lae --create-home --shell /usr/sbin/nologin lae
   sudo git clone https://github.com/azaroth42/linked-art-editor.git /opt/lae
   sudo chown -R lae:lae /opt/lae
   sudo -u lae sh -c 'cd /opt/lae/backend && uv sync --locked --no-dev'
   sudo install -d -o lae -g lae /var/lib/lae/qlever
   ```

3. Create the database and extensions (`vector` needs a superuser):

   ```sh
   sudo -u postgres createuser lae
   sudo -u postgres createdb --owner lae lae
   sudo -u postgres psql -d lae -f /opt/lae/deploy/postgres/init.sql
   ```

4. Write `/etc/lae/lae.env` (`sudo install -d -m 0750 -g lae /etc/lae`, then the file with mode
   `0640`, owner `root`, group `lae`):

   ```sh
   LAE_ENV=production
   # Unix socket with peer authentication (Debian default); or give `lae` a password and use TCP.
   LAE_DATABASE_URL=postgresql+asyncpg://lae@/lae?host=/var/run/postgresql
   LAE_QLEVER_URL=http://localhost:7010
   LAE_QLEVER_UPDATE_URL=http://localhost:7010
   QLEVER_PORT=7010
   LAE_QLEVER_ACCESS_TOKEN=<output of: openssl rand -hex 32>
   # Optional: the sync worker's Prometheus GET /metrics (outbox_pending, outbox_failed,
   # sync_lag_seconds, ...) on 127.0.0.1:<port>; unset means no listener. Alert on
   # outbox_failed > 0 (see "Dead letters in the sync queue" below).
   # LAE_SYNC_METRICS_PORT=9100
   LAE_BASE_URI=https://<your host>/id/
   # Language of the derived record label (_label) and of entity_labels.default_label: one of the
   # 57 primary language tags of the AAT language table (en, fr, de, ...; default en). Give every
   # process the same value (api and crdt-ws above all). To change it, follow "Changing the label
   # language" below: restart api and crdt-ws together, then run `lae relabel` (ADR-0036,
   # ADR-0038). Open drafts follow at their next read.
   # LAE_LABEL_LANGUAGE=en
   # Required in production: signs the session cookie (shared by api and crdt-ws).
   LAE_SESSION_SECRET=<output of: openssl rand -hex 32>
   # Required in production: shared secret of the internal RPC between api (and scheduler)
   # and crdt-ws.
   LAE_INTERNAL_TOKEN=<output of: openssl rand -hex 32>
   # Where crdt-ws listens, and where api reaches it. Proxy /ws/ to it; never expose /internal/.
   # Run exactly one crdt-ws per database: a second one refuses to start (advisory lock).
   LAE_CRDT_WS_HOST=127.0.0.1
   LAE_CRDT_WS_PORT=8001
   LAE_CRDT_WS_URL=http://127.0.0.1:8001
   # Browser origins that may open a draft room, besides the origin of LAE_BASE_URI. Needed only
   # when the editor is served from another origin than the record URIs (comma-separated).
   # LAE_ALLOWED_ORIGINS=https://editor.<your host>
   # Sign-in (docs/dev-setup.md §2); redirect URI https://<your host>/auth/callback.
   LAE_OIDC_ISSUER=https://<your IdP>/realms/<realm>
   LAE_OIDC_CLIENT_ID=<client id>
   LAE_OIDC_CLIENT_SECRET=<client secret>
   # And/or GitHub (docs/dev-setup.md §3); callback URL https://<your host>/auth/callback/github.
   # Without an allowlist any GitHub user can sign in (as Viewer).
   # LAE_GITHUB_CLIENT_ID=<OAuth App client id>
   # LAE_GITHUB_CLIENT_SECRET=<OAuth App client secret>
   # LAE_GITHUB_ALLOWED_ORGS=<organization>
   # LAE_GITHUB_ALLOWED_USERS=<login>,<login>
   # Never set LAE_DEV_USER here: `lae api` refuses to start with it in production.
   ```

   The QLever port must be reachable only from the `lae` hosts (conventions §9). `qlever-server`
   listens on all interfaces and has no bind option, so close the port with a host firewall
   (`ufw`, `nftables`) except for those hosts. It also receives the access token as a
   command-line argument, so local users can see it in the process list; run QLever on a host
   without untrusted local users.

5. Apply the database migrations (repeat after every update, before restarting the units):

   ```sh
   sudo -u lae sh -c 'set -a && . /etc/lae/lae.env && /opt/lae/backend/.venv/bin/lae migrate'
   ```

6. Build the (empty) index once, then install and start the units:

   ```sh
   sudo -u lae sh -c 'cd /var/lib/lae/qlever && set -a && . /etc/lae/lae.env && qlever --qleverfile /opt/lae/deploy/Qleverfile get-data && qlever --qleverfile /opt/lae/deploy/Qleverfile index'
   sudo cp /opt/lae/deploy/systemd/*.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now qlever lae-api lae-crdt-ws lae-sync-worker lae-scheduler
   ```

   `qlever.service` runs `qlever start --run-in-foreground` and stops on `SIGINT`; with
   `PERSIST_UPDATES = true`, SPARQL updates survive restarts. Its `ExecStart` uses
   `/usr/bin/qlever`; change it if `command -v qlever` says otherwise.

### Changing the label language

`LAE_LABEL_LANGUAGE` is read once, when a process starts. To change it:

1. Set `LAE_LABEL_LANGUAGE` in the environment of every process (`/etc/lae/lae.env`).
2. Optional: see what would change. A dry run writes nothing, takes no lock, and may run at any
   time:

   ```sh
   sudo -u lae sh -c 'set -a && . /etc/lae/lae.env && /opt/lae/backend/.venv/bin/lae relabel --dry-run --report /tmp/relabel-dry-run.jsonl'
   ```

3. Restart `api` and `crdt-ws` together: `sudo systemctl restart lae-api lae-crdt-ws`.
4. **After** the restart, recompute `entity_labels.default_label` under the new value (a process
   still running with the old value would write old-language labels behind it):

   ```sh
   sudo -u lae sh -c 'set -a && . /etc/lae/lae.env && /opt/lae/backend/.venv/bin/lae relabel'
   ```

   It prints a JSON summary. Expect exit code 0; exit code 1 means some rows were locked by
   other transactions (or the run failed): run it again. Options: `--batch N` (rows per
   transaction, 1 to 10 000, default 500), `--report PATH` (one JSON line per row changed).
5. No second restart and no QLever rebuild. Do not run it while `lae bootstrap load`,
   `lae bootstrap aat-labels` or a `lae vocab` command runs: their rows would be skipped as
   locked.

`lae relabel` has no language option: it relabels under the value the installation is
configured with. It may be repeated at any time and changes nothing the second time; switching
back and running it again restores the previous default labels. It changes only
`entity_labels.default_label` (what reference labels, autocomplete, the drafts list and the
vocabulary bundle show; running `api` processes pick up the new bundle without a restart). The
Public Reader title follows the setting by itself. **Published records keep their stored
`_label`** (in the raw JSON-LD, the revisions, and `rdfs:label` in QLever) until they are next
published. AAT rows take the AAT label in the new language when they have one, else the English
preferred name (the Phase 1 AAT CSV is English only, so they stay English).

## Dead letters in the sync queue

`sync-worker` applies the outbox to QLever. An event it cannot apply becomes a **dead letter**:
its row in `outbox_events` gets `status = 'failed'` and the cause in `last_error`, and the worker
goes on with the next event. This happens on the live path (an event QLever rejects five times,
each rejection confirmed by a probe update that QLever accepts and a second send it rejects
again, or an event that cannot be translated five times) and in catch-up (a `done` event above a lowered watermark,
after an index rebuild or restore, that QLever now rejects or whose stored revision cannot be
read; `last_error` then has `"catch_up": true`). Only the record of that event falls behind in
QLever; every other record stays current. If QLever rejects the probe too, the endpoint refuses
every update (a wrong `LAE_QLEVER_UPDATE_URL` answering 404 or 405, a proxy): no event is
blamed, nothing is written, and the worker retries with backoff until it is fixed. Contract:
`docs/contracts/1.11-sync-worker.md` §7 and §8 (ADR-0037, ADR-0039).

What to watch (`GET /metrics` needs `LAE_SYNC_METRICS_PORT`; the listener is up before the first
catch-up starts):

| Signal | Where | Meaning |
| :-- | :-- | :-- |
| `outbox_failed` > 0 | sync worker `GET /metrics` | at least one dead letter (live or catch-up). **This is the alarm:** alert on any value above 0 |
| `sync_catch_up_dead_letters_total`, `sync_dead_letters_total` increase | same | a dead letter was just made (in catch-up; in total) |
| `sync_catch_up_events_total` increases | same | a catch-up is replaying (an index was rebuilt or restored) |
| `sync_unavailable_total` grows, `sync_watermark` does not move | same | QLever is down, or refuses every update |
| log `sync_dead_letter`, level error, with `event_id`, `record_id`, `error` and `during` (`catch_up` or `live`) | worker log | one per dead letter |
| log `sync_updates_refused`, level error, with `status`, `body` (token removed, then the first 2000 characters), `consecutive` and `during` (`live` or `catch_up`) | worker log | the probe update was rejected; retried with backoff; no event is touched |
| log `sync_updates_refused` with `during="live"`; `sync_unavailable_total` and `sync_lag_seconds` grow, `outbox_failed` does not | worker log, `GET /metrics` | the endpoint rejects every update; no event is touched; the worker retries by itself |
| log `sync_caught_up`, level info (warning with a dead letter), with `events` and `dead_letters` | worker log | catch-up finished |
| `outbox_events` rows with `status = 'failed'` | Postgres; `lae sync-worker --failed` | the durable list of dead letters and their causes |

What to do:

1. List them: `lae sync-worker --failed` (from `backend/`, `uv run lae sync-worker --failed`; in
   production `sudo -u lae sh -c 'set -a && . /etc/lae/lae.env && /opt/lae/backend/.venv/bin/lae
   sync-worker --failed'`). It prints one JSON line per dead letter (`id`, `kind`, `record_id`,
   `from_version`, `to_version`, `attempts`, `created_at`, `processed_at`, `last_error`), only
   reads, needs no QLever settings, and can run while the worker runs. Each line names the
   record and the cause.
2. `LAE-SYNC-004` (QLever rejected the update; `body` holds its answer): the record is behind in
   QLever by at least one version. Publish the record again with any edit; its next event
   replaces all its triples. Or wait for the next index rebuild, which reads the current data.
3. `LAE-SYNC-001` or `-002` (a revision is missing or unreadable): the damage is in Postgres
   (`record_revisions`), not in QLever. Restore that row from a backup; until then every later
   event of the record is a dead letter too. A rebuild makes the index right for the record's
   current version in the meantime.
4. `LAE-SYNC-003`: the worker is older than the code that wrote the event. Deploy the matching
   version, then treat as 2.
5. `sync_updates_refused` in the log (live or in catch-up): fix `LAE_QLEVER_URL` /
   `LAE_QLEVER_UPDATE_URL` or the server. The worker recovers by itself and no event is lost.
6. Never set a `failed` row back to `pending` or `done` by hand: an old event sent on its own can
   undo a later one.

## When the sync worker stops

`lae sync-worker` exits with one of these codes, and `deploy/systemd/lae-sync-worker.service`
restarts it only after 1 (contract `docs/contracts/1.11-sync-worker.md` §8.5 and §9, ADR-0039):

| Code | When | The unit |
| :-- | :-- | :-- |
| 0 | stopped by `SIGTERM`/`SIGINT` | not restarted |
| 1 | may pass by itself, or unknown: Postgres unreachable, the connection lost, a database error of unknown kind | restarted every 5 s, without limit (`StartLimitIntervalSec=0`) |
| 2 | refuses to start: `LAE_QLEVER_URL` or `LAE_QLEVER_ACCESS_TOKEN` missing, `LAE_DATABASE_URL` cannot be parsed, the metrics port cannot be bound, or the watermark is not an integer | **not restarted** |
| 4 | a database error that will not pass by itself: SQLSTATE class 42 (our SQL against the wrong schema, a missing privilege), 23 (a violated constraint), 22 (bad data), 0A | **not restarted** |

QLever being down or refusing updates never stops the worker: it retries with backoff.

What to watch:

| Signal | Where | Meaning |
| :-- | :-- | :-- |
| unit `lae-sync-worker` is `failed` with exit status 4; the log has `sync_worker_failed` with `persistent=true`, a `sqlstate` and a traceback; `/metrics` no longer answers | `systemctl status`, `journalctl -u lae-sync-worker` | a database error that will not pass by itself; **the worker stays down until someone starts it** |
| unit `failed` with exit status 2; `refusing to start: …` on stderr | same | a setting is missing or the watermark is not an integer; not restarted |
| unit `failed` with exit status 2; `refusing to start: LAE_DATABASE_URL is not a valid PostgreSQL URL: …` on stderr | same | `LAE_DATABASE_URL` cannot be parsed (no scheme, a port that is not a number, a parameter asyncpg refuses); not restarted |
| unit `failed` with exit status 2; `refusing to start: cannot listen for metrics on <host>:<port>: …` on stderr | same | another process holds `LAE_SYNC_METRICS_PORT`, the port needs a privilege, or `LAE_SYNC_METRICS_HOST` is not an address of this host; not restarted |
| unit restarting every 5 s with exit status 1 and `sync_connection_lost` or `sync_database_unreachable` | same | Postgres is away (or its host name does not resolve); the worker comes back by itself |
| unit restarting every 5 s with exit status 1 and `sync_worker_failed` with `persistent=false` | same | a database error of unknown kind keeps coming back: read the traceback |

**The alarm for a stopped worker** is the unit state (`systemctl is-failed lae-sync-worker`) or
a failed scrape of `/metrics`; alert on either. Nothing is lost while it is down: events stay
`pending` and are sent after the start.

What to do:

1. Exit status 4: read the `sync_worker_failed` line. SQLSTATE class 42 with an undefined table
   or column means the schema and the code do not match: run the migrations or deploy the
   matching version. `42501`: grant the `lae` role the privilege. Class 22 or 23: a row the
   worker reads or writes is wrong; report it as a bug with the traceback. Then
   `sudo systemctl start lae-sync-worker`.
2. Exit status 2: fix `/etc/lae/lae.env` (`LAE_DATABASE_URL`, or `LAE_SYNC_METRICS_PORT` and
   `LAE_SYNC_METRICS_HOST`, or stop the process that holds the port) or the
   `qlever_index_watermark` row of `sync_state`, then `sudo systemctl start lae-sync-worker`.
3. After a migration, or a restore into the live database, check the unit and start it if it is
   `failed`: a worker that met the schema half-way has stopped with 4.
4. Installing the changed unit: `sudo cp /opt/lae/deploy/systemd/lae-sync-worker.service
   /etc/systemd/system/`, `sudo systemctl daemon-reload`,
   `sudo systemctl restart lae-sync-worker`.

`lae sync-worker --replay-after` and `--failed` report a database error the same way and exit
with the same codes; `--replay-after` still prints the last event id it applied.
