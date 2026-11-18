# Proposed S3 upload

Upload each validated AP `continuous.dat` with its compact **`timing.json`**, recording metadata, registration provenance, and waveform-validation artifacts. Per the revised instruction, do not generate or upload per-sample timestamp arrays.

The destination bucket is intentionally unspecified. No S3 writes have been performed. [The object manifest](outputs/upload_manifest.csv) supplies local sources and proposed keys; [the summary](outputs/summary.json) gives the current data volume and eligibility counts.

## Object layout

```text
s3://<destination-bucket>/dynamic-gating/DANDI-001051/0.260922.1827/
  session-<LIMS_ID>/probeA/
    continuous.dat
    timing.json
    structure.oebin
    probe_sync_registration.json
    validation.png
    validation.json
  inventory.csv
  sessions.csv
  upload_manifest.csv
  summary.json
```

Repeat the probe folder for each included probe. Keys contain stable LIMS session IDs and probe labels, avoiding collisions between mice, dates, and DANDI assets. Preserve the original sample order, channel order, and int16 encoding. `timing.json` describes exactly that binary's row count and clock. If multiple recording segments are to be retained later, add a segment level to the key and provide a separate timing model for every segment; do not overwrite the selected main recording or concatenate segments implicitly.

## Preparation and eligibility

1. Run the documented discovery, NWB reference, timing, validation, and report stages in [README.md](README.md). Recheck source sizes, modification times, and alignment hashes immediately before transfer.
2. Review every proposed waveform plot, prioritizing `review_required` and multi-recording cases. Only rows with a verified NWB/LIMS identity, an available AP binary, valid timing metadata, and passing waveform validation are automatically marked `eligible_after_plot_review`.
3. Exclude unresolved recordings, `.npx2`-only entries, missing registration, and probes without an eligible NWB unit from the initial upload. Preserve their inventory rows and explanations. Repair missing inputs or investigate timing discrepancies, then regenerate timing metadata and validation; never override flags merely to complete a batch.
4. Set the destination bucket/prefix and storage class when an upload is requested. The current task prepares files and a plan only.

## Transfer procedure

Run the future transfer from an Allen-network machine with reliable access to the original files and the destination bucket. Stream the original binaries directly; do not copy tens of terabytes to this laptop. The compact timing JSONs eliminate the roughly one-byte-of-timestamps-per-96-bytes-of-384-channel-AP overhead of float64 timestamp arrays.

Use a supported S3 transfer client with multipart upload, bounded concurrency, retries, and a persistent per-object transfer ledger. Record the source snapshot, destination key, uploaded byte count, upload ID/version ID, checksum algorithm, and returned checksum. Preserve incomplete upload IDs for explicit resume or cleanup; never mark a partial upload complete. Upload each binary and its metadata under the proposed keys, and publish the final session manifest only after the complete probe set has passed verification. Consumers should use that manifest to identify completed sessions. See the [AWS multipart-upload process](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html).

Use S3's supported checksum verification for the chosen multipart algorithm. A multipart ETag must not be treated as an ordinary MD5 of the complete source file. Verify object length, service checksum, and the ledger before declaring transfer success. Record cryptographic provenance hashes for the small JSON and QC artifacts. If a full-file SHA-256 is required by the destination, compute it while reading the source and retain it separately from any composite multipart checksum. See [AWS full-object and composite checksum semantics](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html).

For each uploaded probe, fetch small byte ranges around some of the saved validation sample indices. Decode them using `timing.json` and compare with the source-derived snippets. Also check reconstructed first/last timestamps and the JSON sample count against the binary object's length. Publish `inventory.csv`, the final transfer ledger, and a manifest listing exactly which objects completed. Retain original source data.

## Limits of this run

The available registration JSONs define an affine clock. Sparse acquisition-counter checks and waveform averages at three recording periods provide useful validation, but cannot exclude every isolated counter discontinuity. The upload preparation stage should retain this limitation explicitly; a complete continuity audit can be added before transfer where required.

Waveform plots are diagnostic evidence, and failed comparisons are an intended output of this project. In particular, a compact timing JSON for a flagged probe is a candidate transform from the existing registration, not approval to upload that recording as correctly synchronized.
