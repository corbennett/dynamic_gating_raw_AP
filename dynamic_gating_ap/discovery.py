"""Read DANDI and LIMS; never change source data or upload anything."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from .common import get_json, write_csv, write_json

DANDISET = "001051"
VERSION = "0.260922.1827"
DANDI = "https://api.dandiarchive.org/api"
LIMS = "https://lims2.corp.alleninstitute.org"
QC_ROOT = "/Volumes/programs-6/braintv/workgroups/nc-ophys/corbettb/NP_behavior_pipeline/QC"


def assets(cache: Path) -> list[dict]:
    url = f"{DANDI}/dandisets/{DANDISET}/versions/{VERSION}/assets/?page_size=100"
    result = []
    expected = None
    while url:
        page = get_json(url, cache)
        if expected is None:
            expected = page["count"]
        result.extend(page["results"])
        url = page["next"]
    if len(result) != expected or len({a["asset_id"] for a in result}) != expected:
        raise ValueError("DANDI pagination count mismatch")
    return result


def mouse_sessions(mouse: str, cache: Path) -> list[dict]:
    donors = get_json(f"{LIMS}/donors/info/details.json?external_donor_name={mouse}", cache)
    sessions = {}
    for donor in donors:
        if str(donor["external_donor_name"]) != mouse:
            raise ValueError("LIMS returned a different mouse")
        for specimen in donor["specimens"]:
            response = get_json(f"{LIMS}/ecephys_sessions.json?specimen_id={specimen['id']}", cache)
            for session in response:
                if session["specimen"]["id"] != specimen["id"]:
                    raise ValueError("LIMS ignored the specimen filter")
                sessions[session["id"]] = session
    return list(sessions.values())


def map_path(path: str, mount: Path) -> Path:
    normalized = path.replace("\\", "/").lstrip("/")
    if normalized.startswith("allen/programs/"):
        return mount / normalized.removeprefix("allen/programs/")
    return Path(path)


def bounded_scan(root: Path, timeout: float = 20, check_only: bool = False) -> dict:
    """Isolate SMB system calls in a killable subprocess, not a stuck thread."""
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "dynamic_gating_ap.scan", str(root)]
            + (["--check-only"] if check_only else []),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            # Do not wait on uninterruptible network filesystem I/O.
            return {"status": "access_timeout", "error": f"No response within {timeout}s: {root}"}
        if process.returncode:
            return {"status": "access_error", "error": stderr[-1000:]}
        return json.loads(stdout)
    except OSError as exc:
        return {"status": "access_error", "error": str(exc)}


def build_inventory(
    output: Path, mount: Path, qc_root: Path, workers: int = 8, scan_files: bool = False
) -> list[dict]:
    cache = output / "cache" / "http"
    catalog = assets(cache)
    write_json(output / "dandi_assets.json", catalog)
    session_assets = [a for a in catalog if re.search(r"_ses-\d{8}T", a["path"])]
    subjects = sorted({re.search(r"sub-([^/]+)", a["path"])[1] for a in session_assets})
    sessions, errors = {}, {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(mouse_sessions, mouse, cache): mouse for mouse in subjects}
        for future in as_completed(futures):
            mouse = futures[future]
            try:
                sessions[mouse] = future.result()
            except Exception as exc:
                errors[mouse] = f"{type(exc).__name__}: {exc}"
    rows = []
    nwb_index = (
        {x["asset_id"]: x for x in json.loads((output / "nwb_index.json").read_text())}
        if (output / "nwb_index.json").exists()
        else {}
    )
    for asset in session_assets:
        match = re.search(r"sub-([^/]+).*_ses-(\d{8})T", asset["path"])
        row = dict(
            dandiset=DANDISET,
            version=VERSION,
            nwb_asset_id=asset["asset_id"],
            nwb_path=asset["path"],
            nwb_url=f"{DANDI}/assets/{asset['asset_id']}/download/",
            nwb_size_bytes=asset["size"],
            subject_id="",
            session_date="",
            lims_session_id="",
            session_match="",
            probe="",
            lims_url="",
            lims_storage_path="",
            local_storage_path="",
            raw_settings_paths="",
            archived_settings_urls="",
            dat_path="",
            dat_uri="",
            alignment_path="",
            alignment_uri="",
            discovery_status="",
            discovery_error="",
            timing_path="",
            timing_status="not_run",
            validation_status="not_run",
            validation_plot="",
            upload_status="not_ready",
        )
        if not match or not asset["path"].endswith(".nwb"):
            row.update(discovery_status="unsupported_asset")
            rows.append(row)
            continue
        mouse, date = match.groups()
        row.update(subject_id=mouse, session_date=date)
        if mouse in errors:
            row.update(discovery_status="lims_unavailable", discovery_error=errors[mouse])
            rows.append(row)
            continue
        candidates = [s for s in sessions[mouse] if re.match(date + r"\d{6}_", s["name"])]
        nwb = nwb_index.get(asset["asset_id"], {})
        if nwb.get("identifier", "").isdigit():
            candidates = get_json(f"{LIMS}/ecephys_sessions.json?id={nwb['identifier']}", cache)
            if any(
                str(s["id"]) != nwb["identifier"]
                or str(s["specimen"]["external_specimen_name"]) != mouse
                for s in candidates
            ):
                raise ValueError("NWB identifier resolved to a different LIMS session or subject")
        if len(candidates) != 1:
            row.update(
                discovery_status="no_lims_match" if not candidates else "ambiguous_lims_match",
                discovery_error="Candidate IDs: " + ",".join(str(s["id"]) for s in candidates),
            )
            rows.append(row)
            continue
        session = candidates[0]
        sid = str(session["id"])
        name_date = re.match(r"(\d{8})", session["name"])
        if name_date:
            date = name_date[1]
            row["session_date"] = date
        folder = f"{sid}_{mouse}_{date}"
        alignment = (
            qc_root / folder / "probeSyncAlignment" / f"{mouse}_{date}_probe_sync_registration.json"
        )
        storage = session["storage_directory"] or ""
        row.update(
            lims_session_id=sid,
            session_match="unique_mouse_and_session_name_date; verify_nwb_identifier",
            lims_url=f"{LIMS}/ecephys_sessions/{sid}",
            lims_storage_path=storage,
            local_storage_path=str(map_path(storage, mount)) if storage else "",
            alignment_path=str(alignment),
            alignment_uri=alignment.absolute().as_uri(),
        )
        if nwb.get("identifier") == sid:
            row["session_match"] = "nwb_identifier_and_subject_verified"
        probes = session["ecephys_probes"]
        if not probes:
            row.update(discovery_status="no_lims_probes")
            rows.append(row)
        for probe in probes:
            reference = nwb.get("probes", {}).get(probe["name"], {})
            companion = [
                a
                for a in catalog
                if a["path"].startswith(f"sub-{mouse}/")
                and f"_probe-{reference.get('probe_id')}_" in a["path"]
            ]
            rows.append(
                dict(
                    row,
                    probe=probe["name"],
                    lims_probe_id=probe["id"],
                    companion_nwb_urls=json.dumps(
                        [f"{DANDI}/assets/{a['asset_id']}/download/" for a in companion]
                    ),
                    discovery_status="pending_file_lookup",
                )
            )

    def lookup(row):
        if row["discovery_status"] != "pending_file_lookup":
            return
        query = urlencode(
            {"attachable_id": row["lims_probe_id"], "attachable_type": "EcephysProbe"}
        )
        records = get_json(f"{LIMS}/well_known_files.json?{query}", cache)
        raw = [r for r in records if r["well_known_file_type"]["name"] == "EcephysProbeRawData"]
        row["raw_settings_paths"] = json.dumps([r["full_filename"] for r in raw])
        row["archived_settings_urls"] = json.dumps(
            [d["url"] for r in raw for d in r.get("digital_resources", []) if d.get("url")]
        )
        row["discovery_status"] = "raw_location_unverified" if raw else "no_lims_raw_record"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(lookup, row): row for row in rows}
        for n, future in enumerate(as_completed(futures), 1):
            try:
                future.result()
            except Exception as exc:
                futures[future].update(
                    discovery_status="lims_file_lookup_error", discovery_error=str(exc)
                )
            if n % 100 == 0:
                print(f"LIMS probe lookups: {n}/{len(rows)}", flush=True)
    write_csv(output / "inventory.csv", rows)
    if scan_files:
        resolve_files(rows, mount, output)
    write_json(
        output / "inventory_summary.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dandiset": DANDISET,
            "version": VERSION,
            "assets": len(catalog),
            "subjects": len(subjects),
            "rows": len(rows),
            "status_counts": {
                s: sum(r["discovery_status"] == s for r in rows)
                for s in sorted({r["discovery_status"] for r in rows})
            },
        },
    )
    return rows


def resolve_files(rows: list[dict], mount: Path, output: Path) -> None:
    # Probe records often share a recording node. Scan each root only once.
    scans = {}
    nwb_index = (
        {x["asset_id"]: x for x in json.loads((output / "nwb_index.json").read_text())}
        if (output / "nwb_index.json").exists()
        else {}
    )
    mount_check = bounded_scan(mount, timeout=10, check_only=True)
    if mount_check["status"] != "ok":
        for row in rows:
            if row["discovery_status"] in ("raw_location_unverified", "no_lims_raw_record"):
                row.update(
                    discovery_status=mount_check["status"],
                    discovery_error=mount_check.get("error", ""),
                )
        write_csv(output / "inventory.csv", rows)
        return
    for row in rows:
        if row["discovery_status"] == "no_lims_probes" and row["local_storage_path"]:
            result = bounded_scan(Path(row["local_storage_path"]))
            row["session_directory_scan_status"] = result["status"]
            row["unregistered_streams_json"] = json.dumps(result.get("streams", []))
            row["compressed_recording_paths"] = json.dumps(result.get("compressed_recordings", []))
            row["discovery_error"] = result.get("error", "") or (
                "No LIMS probe records; session directory scan found "
                f"{len(result.get('streams', []))} AP streams and "
                f"{len(result.get('compressed_recordings', []))} npx2 recordings"
            )
            write_csv(output / "inventory.csv", rows)
            continue
        if row["discovery_status"] not in (
            "raw_location_unverified",
            "no_lims_raw_record",
            "access_timeout",
            "access_error",
            "permission_denied",
        ):
            continue
        roots = [map_path(p, mount).parent for p in json.loads(row["raw_settings_paths"] or "[]")]
        if not roots and row["local_storage_path"]:
            roots = [Path(row["local_storage_path"])]
        candidates = []
        failures = []
        compressed = []
        invalid = []
        for root in roots:
            if str(root) not in scans:
                scans[str(root)] = bounded_scan(root)
            scan = scans[str(root)]
            if scan["status"] != "ok":
                failures.append(scan)
            candidates.extend(x for x in scan.get("streams", []) if x["probe"] == row["probe"])
            compressed.extend(scan.get("compressed_recordings", []))
            invalid.extend(x for x in scan.get("invalid_streams", []) if x["probe"] == row["probe"])
        row["compressed_recording_paths"] = json.dumps(sorted(set(compressed)))
        row["invalid_recordings"] = json.dumps(invalid)
        candidates = list({x["dat_path"]: x for x in candidates}.values())
        if len(candidates) > 1:
            row["all_recordings_json"] = json.dumps(candidates)
            reference = (
                nwb_index.get(row["nwb_asset_id"], {}).get("probes", {}).get(row["probe"], {})
            )
            if reference.get("status") == "reference_ready":
                try:
                    import numpy as np

                    from .timing import read_alignment

                    alignment = read_alignment(Path(row["alignment_path"]), row["probe"])
                    with np.load(reference["reference_path"], allow_pickle=False) as ref:
                        indices = alignment.indices(ref["spike_times"])
                    fitting = [
                        c
                        for c in candidates
                        if np.mean((indices >= 0) & (indices < c["n_samples"])) >= 0.99
                    ]
                    if len(fitting) == 1:
                        candidates = fitting
                        row["recording_selection"] = (
                            "only_recording_covering_99pct_of_selected_unit_spikes; waveform_validation_required"
                        )
                except (OSError, KeyError, ValueError):
                    pass  # Leave multiple_recordings unresolved, with every candidate recorded.
        if len(candidates) == 1 and not failures:
            stream = candidates[0]
            row.update(stream, discovery_status="dat_found", discovery_error="")
            row["dat_uri"] = Path(row["dat_path"]).as_uri()
        elif len(candidates) > 1:
            row.update(
                discovery_status="multiple_recordings", discovery_error=json.dumps(candidates)
            )
        elif failures:
            row.update(
                discovery_status=failures[0]["status"], discovery_error=failures[0].get("error", "")
            )
        else:
            row.update(
                discovery_status="invalid_dat"
                if invalid
                else "npx2_only"
                if compressed
                else "dat_missing",
                discovery_error="No matching valid AP stream in readable raw roots",
            )
        write_csv(output / "inventory.csv", rows)
