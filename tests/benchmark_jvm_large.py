"""One complete large-JAR export and hash-verified resume; no extrapolated timing."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys

from benchmark_jvm_export import measure
from jvm_source_quality import directory_quality


def inspect_outputs(folder):
    counts = Counter()
    resumed = 0
    mismatches = 0
    markers = Counter()
    errors = []
    paths = set()
    latest = {}
    with (folder / "export/manifest.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            latest[record["origin"]] = record
    for record in latest.values():
        counts[record["status"]] += 1
        if record["status"] != "ok":
            if len(errors) < 20:
                errors.append({"origin": record["origin"], "error": record.get("error")})
            continue
        resumed += bool(record.get("resumed"))
        path = folder / "export" / record["output"]
        paths.add(record["output"])
        body = path.read_bytes()
        mismatches += hashlib.sha256(body).hexdigest() != record["output_sha256"]
        text = body.decode("utf-8", errors="replace")
        for marker in ("Method not decompiled", "Could not decompile", "Decompilation failed"):
            markers[marker] += text.count(marker)
    return {"manifest_statuses": dict(counts), "resumed_records": resumed,
            "unique_source_files": len(paths), "hash_mismatches": mismatches,
            "failure_markers": dict(markers), "error_examples": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--cfr-jar", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--heap-mb", type=int, default=512)
    parser.add_argument("--run-timeout", type=float, default=1800)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.threads <= 0 or args.run_timeout <= 0 or not 64 <= args.heap_mb <= 8192:
        parser.error("threads/timeout must be positive and heap size must be 64..8192 MiB")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output directory must be empty")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    cpus = sorted(os.sched_getaffinity(0))[:args.threads] if hasattr(os, "sched_getaffinity") else []
    env = dict(os.environ, ASC_CFR_JAR=str(args.cfr_jar.resolve()), PYTHONHASHSEED="0",
               JAVA_TOOL_OPTIONS="-XX:ActiveProcessorCount=1")
    report = {"input": str(args.input.resolve()), "sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
              "cfr_sha256": hashlib.sha256(args.cfr_jar.read_bytes()).hexdigest(),
              "threads": args.threads, "heap_mb": args.heap_mb, "cpus": cpus, "batch_size": "auto", "samples": 1,
              "method": "Full export then resume; one process each, no speedup extrapolation. "
                        "Each CFR JVM uses one active processor and the configured heap. "
                        "RSS is a 10 ms process-tree sample when psutil is available.",
              "runs": [], "errors": []}
    export_folder = args.output / "first"
    export_folder.mkdir()
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "main.py"), "export",
               str(args.input.resolve()), "--no-resources", "--threads", str(args.threads),
               "--heap-mb", str(args.heap_mb),
               "-o", str(export_folder / "export")]
    for stage in ("export", "resume"):
        folder = export_folder if stage == "export" else args.output / "resume"
        folder.mkdir(exist_ok=True)
        run = measure(command + (["--resume"] if stage == "resume" else []), folder, env, cpus, args.run_timeout)
        run["stage"] = stage
        try:
            # Resume appends history to the same manifest; inspect only new stdout
            # records separately below and hash the resulting managed source set.
            run["outputs"] = inspect_outputs(export_folder)
            run["source_quality"] = directory_quality(export_folder / "export")
            progress = Counter()
            resumed = 0
            with (folder / "stdout.log").open(encoding="utf-8") as stream:
                for line in stream:
                    record = json.loads(line)
                    progress[record["status"]] += 1
                    resumed += bool(record.get("resumed"))
            run["progress_statuses"] = dict(progress)
            run["progress_resumed"] = resumed
            run["summary"] = json.loads((folder / "stderr.log").read_text().splitlines()[-1])
        except (OSError, ValueError, KeyError) as exc:
            run["inspection_error"] = str(exc)
        report["runs"].append(run)
        if run["returncode"] or run.get("inspection_error") or run.get("outputs", {}).get("hash_mismatches"):
            report["errors"].append(f"{stage}: process/output validation failed")
        if stage == "resume" and run.get("progress_resumed", 0) != report["runs"][0].get("progress_statuses", {}).get("ok", 0):
            report["errors"].append("resume: completed sources were not all reused")
        (args.output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"stage": stage, **run}), flush=True)
        if run["timed_out"]:
            break
    return bool(report["errors"])


if __name__ == "__main__":
    sys.exit(main())
