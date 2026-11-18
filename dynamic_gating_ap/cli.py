"""Resumable stages. No command performs an S3 upload."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .common import read_csv, write_csv, write_json
from .discovery import QC_ROOT, build_inventory, resolve_files
from .nwb import extract_reference, session_assets


def _reference_task(asset, output, legacy):
    try:
        return extract_reference(asset, output, legacy)
    except Exception as exc:
        return dict(
            asset_id=asset["asset_id"], status="nwb_error", error=f"{type(exc).__name__}: {exc}"
        )


def references(output: Path, legacy: bool, workers: int):
    catalog = json.loads((output / "dandi_assets.json").read_text())
    assets = session_assets(catalog)
    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_reference_task, a, output, legacy): a for a in assets}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            write_json(output / "nwb_index.json", sorted(results, key=lambda r: r["asset_id"]))
            print(f"NWB references: {len(results)}/{len(assets)} {result['status']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("outputs"))
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover", help="Build DANDI/LIMS inventory")
    discover.add_argument("--mount", type=Path, default=Path("/Volumes/programs-7"))
    discover.add_argument(
        "--qc-root", type=Path, default=Path(QC_ROOT.replace("programs-6", "programs-7"))
    )
    discover.add_argument("--scan", action="store_true")
    discover.add_argument("--workers", type=int, default=8)
    scan = sub.add_parser("scan", help="Resolve raw AP streams from inventory")
    scan.add_argument("--mount", type=Path, default=Path("/Volumes/programs-7"))
    refs = sub.add_parser("references", help="Stream selected units and waveforms from NWB")
    refs.add_argument("--legacy-qc", action="store_true")
    refs.add_argument("--workers", type=int, default=4)
    validate = sub.add_parser("validate", help="Compare raw spike-triggered averages to NWB")
    validate.add_argument("--workers", type=int, default=4)
    validate.add_argument("--max-spikes", type=int, default=150)
    validate.add_argument("--pre-samples", type=int, default=20)
    validate.add_argument("--force", action="store_true")
    sub.add_parser("timing", help="Write compact master-clock start time and sample rate JSONs")
    sub.add_parser("report", help="Refresh run summary and review index")
    args = parser.parse_args()
    if args.command == "discover":
        build_inventory(args.output, args.mount, args.qc_root, args.workers, args.scan)
    elif args.command == "scan":
        rows = read_csv(args.output / "inventory.csv")
        resolve_files(rows, args.mount, args.output)
    elif args.command == "references":
        references(args.output, args.legacy_qc, args.workers)
    elif args.command == "validate":
        from .validation import validate_inventory

        validate_inventory(args.output, args.workers, args.max_spikes, args.pre_samples, args.force)
    elif args.command == "timing":
        from .timing import write_timing_metadata

        rows = read_csv(args.output / "inventory.csv")
        for row in rows:
            if row["discovery_status"] != "dat_found":
                row["timing_status"] = "unavailable_" + row["discovery_status"]
                continue
            target = args.output / "timing" / row["lims_session_id"] / (row["probe"] + ".json")
            try:
                metadata = write_timing_metadata(
                    Path(row["dat_path"]),
                    int(row["n_channels"]),
                    Path(row["alignment_path"]),
                    row["probe"],
                    target,
                    row["lims_session_id"],
                    row["nwb_asset_id"],
                    sample_numbers=Path(row["sample_numbers_path"])
                    if row.get("sample_numbers_path")
                    else None,
                )
                row.update(
                    timing_path=str(target.absolute()),
                    timing_status="generated",
                    timing_error="",
                    start_time=metadata["start_time"],
                    sample_rate=metadata["sample_rate"],
                )
            except Exception as exc:
                row.update(timing_status="error", timing_error=f"{type(exc).__name__}: {exc}")
            write_csv(args.output / "inventory.csv", rows)
            print(f"{row['lims_session_id']}/{row['probe']}: {row['timing_status']}", flush=True)
        write_csv(args.output / "inventory.csv", rows)
    elif args.command == "report":
        from .report import report

        report(args.output)


if __name__ == "__main__":
    main()
