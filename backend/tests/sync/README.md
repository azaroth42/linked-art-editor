# Sync worker tests (Phase 1 Step 1.11)

Contract: `docs/contracts/1.11-sync-worker.md` §5.

| File | Tests | Needs |
| :-- | :-- | :-- |
| `test_events.py` | 1–10 | PostgreSQL |
| `test_batch.py` | 11–27 | PostgreSQL |
| `test_replay_property.py` | 28–29 | nothing (hypothesis, pyoxigraph) |
| `test_qlever_integration.py` | 30–37 | PostgreSQL and a real QLever 0.6.0 |

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
