# Sync worker tests (Phase 1 Step 1.11)

Contract: `docs/contracts/1.11-sync-worker.md` §5, §7.7 for issue #150 (catch-up meets a
`done` event it cannot replay), and §8.9 for issue #171 (the live path confirms a rejection with
the probe; exit codes and the systemd unit; `--replay-after` database errors; redaction).

| File | Tests | Needs |
| :-- | :-- | :-- |
| `test_events.py` | 1–10 | PostgreSQL |
| `test_batch.py` | 11–27; 38–53 (§7: the poisoned world, the probe, dead letters in catch-up, `--failed`); 56–62, 71, 72 (§8: the live probe, refusals, the token across the cut or JSON-escaped) | PostgreSQL |
| `test_replay_property.py` | 28–29; 54 (replaying around dead letters) | nothing (hypothesis, pyoxigraph) |
| `test_qlever_integration.py` | 30–37; 55 (QLever's own rejection in catch-up, and the probe); 63 (the same on the live path; the probe adds no delta) | PostgreSQL and a real QLever 0.6.0 |
| `test_connection_loss.py` | issue #151: how `run` exits on a database error (§3 item 22, §4); 64–70 (§8.5: the three kinds of database error, exit 4, the unit's restart rules, `--replay-after` and `--failed`) | PostgreSQL |

**The poisoned world** (`test_batch.poisoned_world`, contract §7.7): three records published and
processed live; record R's version 2 adds a marker literal and version 3 removes it. The
watermark is then lowered and the client rejects every update that contains the marker, so
catch-up meets a `done` event it cannot replay. The recording client keeps `acknowledged` (the
updates that did not raise) besides `sent`; a behaviour tells a probe by
`sparql == PROBE_UPDATE`. `logged` (in `conftest.py`) captures the worker's JSON log lines.

The QLever tests skip, with a reason, when `qlever` (qlever-control) or `qlever-index` is not on
`PATH` or `LAE_TEST_QLEVER_PORT` is unset. With `LAE_REQUIRE_QLEVER=1` they fail instead of
skipping.

## Running the QLever tests locally

Install QLever 0.6.0 from Homebrew (`deploy/QLEVER_VERSION`; Homebrew 7 needs the tap trusted):

```sh
brew tap qlever-dev/qlever && brew trust qlever-dev/qlever && brew install qlever-dev/qlever/qlever
qlever-server --version   # qlever-server v0.6.0
```

Then, from `backend/`, with your slot's test database and QLever port (`docs/agent-workflow.md`
§3; slot `N` uses `lae_i<N>_test` and port `7000+N`):

```sh
LAE_TEST_DATABASE_URL=postgresql+asyncpg://localhost:5432/lae_i<N>_test \
LAE_TEST_QLEVER_PORT=70<NN> LAE_REQUIRE_QLEVER=1 \
uv run pytest -m integration tests/sync -q -rs
```

Each test builds a fresh index in a temporary directory with `deploy/Qleverfile`, serves it on
that port with a random access token, and stops exactly that server afterwards
(`lae.bootstrap.index.stop_command`). Never run a bare `qlever stop`: it stops every QLever on
the machine. The port must be free when the run starts.
