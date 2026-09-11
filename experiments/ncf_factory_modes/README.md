# Fixed-QASM NCF factory placement experiment

All executable code and dependencies run from this independent MiniFlash
checkout. NCF is only a read-only source of previously compiled QASM and
provenance. The fixed corpus is the 106-case intersection of six NCF methods
in the active145 report, producing 636 inputs and 2,544 four-mode outcomes.
The upstream NCF method retains its reference-only label.

Create the independent environment with Python 3.10, install `requirements.lock`
and `plot-requirements.lock`, then this checkout (`pip install --no-deps -e .`), then run the upstream LaSsynth
and seed-cache download scripts. A temporary directory inside the checkout can
be selected with `TMPDIR` if the system temporary filesystem quota is full.
`seed_manifest.json` verifies all 469 cells from the SHA-pinned public archive.

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python experiments/ncf_factory_modes/campaign.py prepare \
  --run experiments/results/fabrics_INPUT_TIMESTAMP
.venv/bin/python experiments/ncf_factory_modes/campaign.py submit \
  --run experiments/results/fabrics_INPUT_TIMESTAMP
```

`prepare` verifies the original manifest, status, checkpoint, postprocess
result and QASM SHA chain. It selects `postprocess_status=ok`, not the overall
unit status: a DASCOT timeout must not exclude a usable compiled circuit.
Original QASM and the exact gate-set normalization are stored read-only.
Normalization expands SX/SXdg and SWAP exactly and removes identity/barrier
operations. No synthesis, optimization, epsilon change or QCEC rerun occurs.
Actual source synthesis precision is 1e-8; the original estimator profile's
unrelated epsilon field is not used to resynthesize the input.

The immutable manifest binds worker sources, the independent interpreter,
package versions, source provenance and each input hash. Use `fork --reuse
OLD_RUN --run NEW_RUN` to reuse immutable inputs with a new code revision;
never overwrite a run manifest or accept stale result shards after an edit.

The Slurm array has 636 tasks, each executing four modes serially in separate
subprocesses with per-mode checkpointing, 7,200-second limits, 1 CPU and 32 GB.
It reserves 8h15m and disables implicit whole-task retries. Each subprocess
uses one BLAS/OpenMP thread. Source/cache preflight runs in every worker.
A changed checkout is rejected even if a job has been queued for some time.
Keep the checkout fixed while its run is active.

The submitter records array/finalizer IDs, installs an `afterany` finalizer,
and starts a lightweight tmux monitor (experiment compute runs on Slurm).
The monitor writes `monitor.jsonl` and `monitor_status.json` every four minutes.
Monitor `squeue`, `sacct`, logs and result shards; recover infrastructure failures
with `submit --indices 1,7,23`. Successful mode results are reused only when
bound input and manifest hashes match. Missing/failed outcomes prevent the
final summary from claiming successful completion. Scientific timeouts remain
visible and do not become zero-valued resource estimates.

Run `aggregate --run RUN` at any time for `results.csv`, `summary.json` and
`report.md`. Statistics are arithmetic means on the same successful
benchmark-method pairs. The physical comparison is the three explicit
fabrics; auto is a distinct legacy reference and its value is labeled
`legacy_physical_qubit_seconds`. Reserve F=ceil(final width/2) sites in each
explicit fabric; limited in-place enables at most two. Each enabled source
has the same 15-to-1 geometry, period and finite storage model.

For a small representative input, `worker --run RUN --task N --render` writes
its glTF files and full selected layout JSON beside the result. Large tasks
normally retain compact statistics instead of expanding their full 3-D scenes.
The source lineage of the fixed NCF SCC partition repair and environment lock
is recorded in `imported_sources.json`; no runtime import uses NCF's vendor.
