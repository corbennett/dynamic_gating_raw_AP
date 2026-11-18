"""Generate review artifacts and an upload manifest; perform no uploads."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from .common import read_csv, write_csv, write_json


def report(output: Path):
    rows = read_csv(output / "inventory.csv")
    catalog = json.loads((output / "dandi_assets.json").read_text())
    sessions = defaultdict(list)
    manifest = []
    timing_index = {}
    for row in rows:
        sessions[row["nwb_asset_id"]].append(row)
        row["compressed_recording_uris"] = json.dumps(
            [
                Path(path).absolute().as_uri()
                for path in json.loads(row.get("compressed_recording_paths") or "[]")
            ]
        )
        if row.get("validation_plot"):
            metrics = json.loads(Path(row["validation_plot"]).with_suffix(".json").read_text())
            failures = []
            if metrics.get("correlation", 1) < 0.8:
                failures.append("overall correlation < 0.8")
            if abs(metrics.get("best_lag_samples", 0)) > 2:
                failures.append("overall lag > 2 samples")
            for i, third in enumerate(metrics.get("thirds", []), 1):
                if third["correlation"] < 0.7 or abs(third["best_lag_samples"]) > 2:
                    failures.append(
                        f"third {i}: r={third['correlation']:.3f}, lag={third['best_lag_samples']} samples"
                    )
            if metrics.get("control_ptp_ratio", 0) >= 0.5:
                failures.append("control amplitude >= 50% of STA")
            if metrics.get("in_bounds_fraction", 1) < 0.99:
                failures.append("fewer than 99% of spikes in bounds")
            row["validation_details"] = "; ".join(failures)
        for path_column, uri_column in (
            ("timing_path", "timing_uri"),
            ("validation_plot", "validation_plot_uri"),
            ("structure_oebin_path", "structure_oebin_uri"),
            ("local_storage_path", "local_storage_uri"),
        ):
            row[uri_column] = (
                Path(row[path_column]).absolute().as_uri() if row.get(path_column) else ""
            )
        timing_path = Path(row["timing_path"]) if row.get("timing_path") else None
        if timing_path and timing_path.exists():
            timing = json.loads(timing_path.read_text())
            timing["waveform_validation"] = {
                "status": row["validation_status"],
                "plot": row["validation_plot"],
                "qc_source": row.get("qc_source", ""),
                "note": "Sparse waveform validation; full sample-counter continuity was not scanned.",
            }
            write_json(timing_path, timing)
            if "start_time" in timing:
                timing_index.setdefault(row["lims_session_id"], {})[row["probe"]] = {
                    key: timing[key] for key in ("start_time", "sample_rate", "n_samples")
                } | {"timing_path": str(timing_path), "validation_status": row["validation_status"]}
        ready = (
            row["discovery_status"] == "dat_found"
            and row.get("timing_status") == "generated"
            and row["validation_status"] == "passed"
            and row["session_match"] == "nwb_identifier_and_subject_verified"
        )
        if ready:
            # Reject stale source snapshots instead of offering a stale upload plan.
            timing_path = Path(row["timing_path"])
            try:
                timing = json.loads(timing_path.read_text())
                dat_stat = Path(row["dat_path"]).stat()
                validation = json.loads(
                    Path(row["validation_plot"]).with_suffix(".json").read_text()
                )
                current_alignment_hash = hashlib.sha256(
                    Path(row["alignment_path"]).read_bytes()
                ).hexdigest()
                ready = (
                    validation.get("status") == "passed"
                    and dat_stat.st_size == timing["dat_size_bytes"]
                    and dat_stat.st_mtime_ns == timing["dat_mtime_ns"]
                    and timing["alignment_sha256"] == current_alignment_hash
                    and validation["signature"]["alignment_sha256"] == current_alignment_hash
                    and validation["signature"]["dat_size"] == dat_stat.st_size
                    and validation["signature"]["dat_mtime"] == dat_stat.st_mtime_ns
                )
            except (OSError, KeyError, ValueError):
                ready = False
        row["upload_status"] = "eligible_after_plot_review" if ready else "not_ready"
        if row["discovery_status"] != "dat_found":
            continue
        prefix = f"dynamic-gating/DANDI-001051/0.260922.1827/session-{row['lims_session_id']}/{row['probe']}"
        files = [
            ("raw_ap", row["dat_path"], "continuous.dat"),
            ("clock_metadata", row.get("timing_path", ""), "timing.json"),
            ("clock_registration", row["alignment_path"], "probe_sync_registration.json"),
            ("recording_structure", row.get("structure_oebin_path", ""), "structure.oebin"),
            ("waveform_plot", row["validation_plot"], "validation.png"),
        ]
        if row["validation_plot"]:
            files.append(
                (
                    "waveform_metrics",
                    str(Path(row["validation_plot"]).with_suffix(".json")),
                    "validation.json",
                )
            )
        for role, path, name in files:
            if path:
                manifest.append(
                    dict(
                        session_id=row["lims_session_id"],
                        probe=row["probe"],
                        role=role,
                        local_path=path,
                        local_uri=Path(path).absolute().as_uri(),
                        proposed_s3_key=f"{prefix}/{name}",
                        eligible=ready,
                        validation_status=row["validation_status"],
                        timing_status=row.get("timing_status", "not_run"),
                        bytes=row.get("dat_size_bytes", "") if role == "raw_ap" else "",
                    )
                )
    session_rows = []
    for asset_id, group in sessions.items():
        first = group[0]
        session_rows.append(
            dict(
                nwb_asset_id=asset_id,
                nwb_url=first["nwb_url"],
                lims_session_id=first["lims_session_id"],
                subject_id=first["subject_id"],
                session_date=first["session_date"],
                probe_rows=len(group),
                dat_found=sum(r["discovery_status"] == "dat_found" for r in group),
                validation_passed=sum(r["validation_status"] == "passed" for r in group),
                discovery_statuses=json.dumps(dict(Counter(r["discovery_status"] for r in group))),
                validation_statuses=json.dumps(
                    dict(Counter(r["validation_status"] for r in group))
                ),
            )
        )
    write_csv(output / "inventory.csv", rows)
    write_json(output / "timing_index.json", timing_index)
    write_csv(output / "sessions.csv", session_rows)
    write_csv(output / "upload_manifest.csv", manifest)
    write_csv(
        output / "review_required.csv",
        [
            {
                key: row.get(key, "")
                for key in (
                    "lims_session_id",
                    "probe",
                    "validation_status",
                    "validation_details",
                    "validation_error",
                    "timing_error",
                    "validation_plot",
                    "alignment_path",
                    "dat_path",
                )
            }
            for row in rows
            if row["validation_status"] in ("review_required", "validation_error")
            or row.get("timing_status") == "error"
        ],
    )
    summary = dict(
        generated_at=datetime.now(timezone.utc).isoformat(),
        dandiset="001051",
        version="0.260922.1827",
        assets=len(catalog),
        sessions=len(sessions),
        probe_rows=len(rows),
        discovery=dict(Counter(r["discovery_status"] for r in rows)),
        validation=dict(Counter(r["validation_status"] for r in rows)),
        timing=dict(Counter(r.get("timing_status", "not_run") for r in rows)),
        discovered_ap_bytes=sum(int(r.get("dat_size_bytes") or 0) for r in rows),
        eligible_probes=sum(r["upload_status"] == "eligible_after_plot_review" for r in rows),
        timestamp_arrays_retained=False,
        s3_uploads_performed=False,
    )
    write_json(output / "summary.json", summary)
    links = []
    for row in rows:
        label = escape(f"{row['lims_session_id']} / {row['probe']} — {row['validation_status']}")
        if row["validation_plot"]:
            relative = Path(row["validation_plot"]).relative_to(output.absolute()).as_posix()
            label = f'<a href="{escape(relative)}">{label}</a>'
        links.append(
            f"<tr><td>{label}</td><td>{escape(row['discovery_status'])}</td>"
            f"<td>{escape(row.get('waveform_correlation', ''))}</td>"
            f"<td>{escape(row.get('validation_details', '') or row.get('validation_error', '') or row.get('timing_error', '') or row.get('discovery_error', ''))}</td></tr>"
        )
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Dynamic Gating AP validation</title>'
        "<style>body{font:15px system-ui;margin:3em;max-width:1300px}td,th{padding:.4em;text-align:left;"
        "border-bottom:1px solid #ddd}pre{background:#f6f6f6;padding:1em}a{color:#1769aa}</style>"
        "<h1>Dynamic Gating AP validation</h1>"
        "<p>Links open unshifted spike-triggered waveform comparisons. Legacy QC fallback is explicitly labeled. "
        "Review flagged plots before upload. Timing JSONs replace per-sample timestamp arrays.</p>"
        f"<pre>{escape(json.dumps(summary, indent=2))}</pre>"
        '<p><a href="inventory.csv">Probe inventory CSV</a> · <a href="sessions.csv">Session CSV</a> · '
        '<a href="upload_manifest.csv">Proposed upload manifest</a></p>'
        "<table><thead><tr><th>Session / probe / validation</th><th>Availability</th><th>Correlation</th>"
        "<th>Details</th></tr></thead><tbody>" + "".join(links) + "</tbody></table>"
    )
