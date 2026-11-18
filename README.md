# Dynamic Gating raw AP staging

This project inventories DANDIset **001051 / 0.260922.1827**, locates raw AP recordings through LIMS, and compares raw spike-triggered averages with NWB waveforms. It prepares a reviewable S3 upload plan; it does not upload data.

**Updated requirement:** per the November 17, 2026 instruction, compact JSON files containing the master-clock start time and sample rate replace per-sample `.npy` timestamp files. No timestamp arrays are retained.

## Results

- [Review index](outputs/index.html): waveform plots, status counts, and links.
- [Probe inventory](outputs/inventory.csv): DANDI, LIMS, AP, alignment, timing, and validation paths, including unavailable records.
- [Session summary](outputs/sessions.csv): one row per NWB session.
- [Timing index](outputs/timing_index.json) and [per-probe metadata](outputs/timing): compact master-clock parameters and provenance.
- [Run summary](outputs/summary.json): machine-readable counts and data volume.
- [Review list](outputs/review_required.csv): the specific checks that failed and missing-registration cases.
- [S3 upload plan](UPLOAD_PLAN.md) and [proposed object manifest](outputs/upload_manifest.csv).

## Reproduce

Use Python 3.11+ and `uv`. The package versions are pinned in `uv.lock`.

```sh
uv sync --locked
uv run dynamic-gating-ap discover
uv run dynamic-gating-ap references --legacy-qc --workers 6
uv run dynamic-gating-ap discover --scan --mount /Volumes/programs-7
uv run dynamic-gating-ap timing
uv run dynamic-gating-ap validate --workers 12
uv run dynamic-gating-ap report
uv run pytest -q
uv run ruff check .
```

Run these stages sequentially: `inventory.csv` is their shared checkpoint. If the share mount changes, provide both `--mount` and `--qc-root` to `discover`. Linux callers can supply `/allen/programs` and the corresponding QC directory. `--output DIR` is a global option and goes before the subcommand. `uv run main.py ...` is an equivalent entry point.

The first discovery pass fetches the asset catalog. The reference pass reads NWB identifiers and selected units via HTTP range requests. The second discovery pass uses those identifiers to resolve exact LIMS sessions, avoiding ambiguous mouse/date matches. HTTP responses and selected NWB reference arrays are cached under `outputs/cache/`; plots and snippets are under `outputs/validation/`. These larger artifacts are ignored by Git but remain available in this workspace. Remote filesystem scans run in bounded subprocesses so a stale SMB mount does not indefinitely block discovery.

## Clock metadata

For zero-based row index `i` in the interleaved little-endian int16 binary:

```python
t_sync_seconds = timing["start_time"] + i / timing["sample_rate"]
sample_index = round((t_sync_seconds - timing["start_time"]) * timing["sample_rate"])
```

`start_time` is the first AP sample's time relative to the master sync clock, not a wall-clock timestamp. It equals **minus** the QC JSON's `shift`. `sample_rate` is the adjusted rate in Hz, not nominal 30 kHz. Each JSON also records sample count, channel count, binary layout, first/last-time semantics, source file identity, registration hash, and validation status. Source AP files are opened read-only.

The formula follows [`npc_ephys.get_adjusted_timestamps`](https://github.com/AllenInstitute/npc_ephys/blob/e8bc6608856ed5107cb4f287f476bc78a656799a/src/npc_ephys/openephys.py). The saved `sample_rate` / `shift` schema and subtraction convention are documented by the pinned [`np_pipeline_qc` registration writer](https://github.com/AllenInstitute/np_pipeline_qc/blob/75ec95fc2563075fa38b5d3e3185a9ce519bd620/src/np_pipeline_qc/legacy/analysis.py) and [spike-time conversion](https://github.com/AllenInstitute/np_pipeline_qc/blob/75ec95fc2563075fa38b5d3e3185a9ce519bd620/src/np_pipeline_qc/legacy/probeSync_qc.py). LIMS discovery uses the read-only endpoints and raw-file types used by [`np_session`](https://github.com/AllenInstitute/np_session/tree/16f70dcda65dfdb19c05ad75bff3ca43a4812288/src/np_session/databases).

Sample counts come from binary length divided by `2 * n_channels`, with channel counts and gains read from `structure.oebin`. Up to 16 evenly spaced acquisition-counter samples, including both endpoints, are checked when `sample_numbers.npy` exists. This is explicitly a **sparse check**, not proof of continuity at every sample. An absolute acquisition-counter origin is recorded as provenance and is not added to the file-relative clock formula.

## Waveform validation

Select the largest-amplitude eligible unit on each probe. Prefer the NWB's boolean `default_qc` column. When it is absent, `--legacy-qc` enables the user-approved fallback: `quality == "good"`, `amplitude_cutoff <= 0.1`, `presence_ratio >= 0.95`, and `isi_violations <= 0.5`. Every result records which rule was used.

Map `peak_channel_id` through the NWB electrode table to `probe_channel_number`; these global electrode IDs are not raw channel indices. Read up to 150 short AP windows divided across early, middle, and late thirds of the recording. Apply common-median referencing and pre-spike baseline subtraction. Compare the stored 82-sample waveform with a 20-sample pre-spike window. The legacy NWB amplitude-unit annotation is not trusted for absolute scaling, so waveform comparison uses normalized shape; raw traces use the Open Ephys gain. No time shift is applied to improve the match.

Automated screening requires overall correlation ≥0.8, each temporal-third correlation ≥0.7, best lag within 2 samples for all comparisons, ≥99% of the selected unit's spikes in bounds, and a +50 ms control amplitude below half the STA amplitude. These are screening thresholds, not a claim that the complete recording has been validated. Flags remain visible for manual inspection. Missing alignment, missing units, failed QC, and inaccessible data are separate outcomes.

Multiple recordings are retained in `all_recordings_json`. A recording is selected only when it alone contains at least 99% of the selected unit's predicted sample indices; it must still pass waveform validation. Ambiguous recordings are excluded. `.npx2`-only recordings are reported without silently decompressing them. A future conversion would need new channel/clock provenance and fresh validation.
