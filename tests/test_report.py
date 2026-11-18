import hashlib
import json

from dynamic_gating_ap.common import read_csv, write_csv, write_json
from dynamic_gating_ap.report import report


def test_changed_alignment_cannot_be_marked_upload_eligible(tmp_path):
    dat = tmp_path / "continuous.dat"
    dat.write_bytes(b"\0" * 40)
    alignment = tmp_path / "alignment.json"
    alignment.write_text("{}")
    digest = hashlib.sha256(alignment.read_bytes()).hexdigest()
    timing = tmp_path / "timing.json"
    write_json(
        timing,
        dict(dat_size_bytes=40, dat_mtime_ns=dat.stat().st_mtime_ns, alignment_sha256=digest),
    )
    plot = tmp_path / "validation.png"
    plot.write_bytes(b"plot")
    write_json(
        plot.with_suffix(".json"),
        dict(
            status="passed",
            signature=dict(alignment_sha256=digest, dat_size=40, dat_mtime=dat.stat().st_mtime_ns),
        ),
    )
    write_json(tmp_path / "dandi_assets.json", [{"asset_id": "asset"}])
    write_csv(
        tmp_path / "inventory.csv",
        [
            dict(
                nwb_asset_id="asset",
                nwb_url="https://example.test/asset",
                lims_session_id="123",
                subject_id="456",
                session_date="20230101",
                probe="probeA",
                discovery_status="dat_found",
                session_match="nwb_identifier_and_subject_verified",
                timing_status="generated",
                timing_path=str(timing),
                validation_status="passed",
                validation_plot=str(plot),
                dat_path=str(dat),
                alignment_path=str(alignment),
                dat_size_bytes="40",
            )
        ],
    )
    report(tmp_path)
    assert read_csv(tmp_path / "inventory.csv")[0]["upload_status"] == "eligible_after_plot_review"
    alignment.write_text('{"changed": true}')
    report(tmp_path)
    assert read_csv(tmp_path / "inventory.csv")[0]["upload_status"] == "not_ready"
    assert json.loads((tmp_path / "summary.json").read_text())["eligible_probes"] == 0
