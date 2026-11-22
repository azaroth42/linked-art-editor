"""Spike S6: bulk bootstrap timing (Phase 0, Step 0.11). Throwaway prototype.

Stages, each run from the backend environment (``cd backend``) and each printing a JSON summary;
results and the method are in ``docs/spikes/S6-bootstrap.md``::

    uv run python ../scripts/spikes/s6_bootstrap/generate.py --count 1000000 --out <file.jsonl>
    uv run python ../scripts/spikes/s6_bootstrap/load.py --input <file.jsonl>
    uv run python ../scripts/spikes/s6_bootstrap/export_nt.py --out <file.nt>
    uv run python ../scripts/spikes/s6_bootstrap/build_index.py index --nt <file.nt> --dir <dir>
    uv run python ../scripts/spikes/s6_bootstrap/build_index.py serve-check --dir <dir> --port <p>

Tests: ``uv run pytest -c pyproject.toml ../scripts/spikes/s6_bootstrap -q``.
"""
