"""Paired CFR process/worker exports with exact per-origin source-hash checks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys

from benchmark_jvm_export import measure
from jvm_source_quality import directory_quality, worker_stack_comment_equivalent

ROOT = Path(__file__).resolve().parents[1]


def inspect_export(folder):
    records = {}
    sources = {}
    mismatches = 0
    for line in (folder / "export/manifest.jsonl").read_text().splitlines():
        record = json.loads(line)
        if record["status"] != "ok":
            raise ValueError(record.get("error", "failed export"))
        body = (folder / "export" / record["output"]).read_bytes()
        value = hashlib.sha256(body).hexdigest()
        mismatches += value != record["output_sha256"]
        records[record["origin"]] = value
        sources[record["origin"]] = folder / "export" / record["output"]
    if mismatches:
        raise ValueError(f"{mismatches} manifest/source hash mismatches")
    return records, sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--cfr-jar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--heap-mb", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--no-enum-switch", action="store_true")
    args = parser.parse_args()
    if args.samples < 3 or args.threads < 1 or args.timeout <= 0:
        parser.error("need >=3 samples and positive threads/timeout")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output directory must be empty")
    args.output.mkdir(parents=True, exist_ok=True)
    cpus = sorted(os.sched_getaffinity(0))[:args.threads] if hasattr(os, "sched_getaffinity") else []
    env = dict(os.environ, ASC_CFR_JAR=str(args.cfr_jar.resolve()), PYTHONHASHSEED="0",
               JAVA_TOOL_OPTIONS="-XX:ActiveProcessorCount=1")
    report = {"input": str(args.input.resolve()), "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
              "cfr_sha256": hashlib.sha256(args.cfr_jar.read_bytes()).hexdigest(),
              "samples": args.samples, "cpus": cpus, "runs": [], "errors": [],
              "method": "Excluded warmup then alternating process/worker pairs; fresh Python process each run. "
                        "Same CFR options, CPU set, worker count and heap. 10ms sampled sum of process-tree RSS. "
                        "Exact source hashes by input origin; separately classify differences confined to host "
                        "frames in CFR stack comments. No semantic-equivalence or significance claim."}
    expected = None
    expected_sources = None
    for sample in range(args.samples + 1):
        for mode in (("process", "worker") if sample % 2 == 0 else ("worker", "process")):
            folder = args.output / f"{sample}-{mode}"
            folder.mkdir()
            command = [sys.executable, str(ROOT / "main.py"), "export", str(args.input.resolve()),
                       "--no-resources", "--cfr-mode", mode, "--threads", str(args.threads),
                       "--batch-size", str(args.batch_size), "--heap-mb", str(args.heap_mb),
                       "-o", str(folder / "export")]
            if args.no_enum_switch:
                command += ["--no-enum-switch"]
            run = {"sample": sample, "warmup": sample == 0, "mode": mode,
                   **measure(command, folder, env, cpus, args.timeout)}
            try:
                if run["returncode"] or run["timed_out"]:
                    raise ValueError("export process failed or timed out")
                hashes, sources = inspect_export(folder)
                if expected is None:
                    expected = hashes
                    expected_sources = sources
                if hashes.keys() != expected.keys():
                    raise ValueError("process/worker origin sets differ")
                changed = [origin for origin in hashes if hashes[origin] != expected[origin]]
                for origin in changed:
                    if not worker_stack_comment_equivalent(expected_sources[origin].read_text(encoding="utf-8"),
                                                           sources[origin].read_text(encoding="utf-8")):
                        raise ValueError(f"process/worker source differs beyond host stack comments: {origin}")
                run["source_files"] = len(hashes)
                run["hash_mismatches"] = 0
                run["exact_baseline_hash_matches"] = len(hashes) - len(changed)
                run["worker_stack_comment_differences"] = len(changed)
                run["summary"] = json.loads((folder / "stderr.log").read_text().splitlines()[-1])
                run["source_quality"] = directory_quality(folder / "export")
            except (OSError, ValueError, KeyError) as exc:
                run["error"] = str(exc)
                report["errors"].append(str(exc))
            report["runs"].append(run)
            (args.output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(run), flush=True)
    report["medians"] = {}
    for mode in ("process", "worker"):
        runs = [r for r in report["runs"] if r["mode"] == mode and not r["warmup"] and "error" not in r]
        if len(runs) == args.samples:
            report["medians"][mode] = {
                "seconds": statistics.median(r["seconds"] for r in runs),
                "rss_bytes": statistics.median(r["sampled_peak_tree_rss_bytes"] for r in runs)
                             if all(r["sampled_peak_tree_rss_bytes"] is not None for r in runs) else None}
    if len(report["medians"]) == 2:
        report["speedup"] = report["medians"]["process"]["seconds"] / report["medians"]["worker"]["seconds"]
    (args.output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("medians", "errors")}), flush=True)
    return bool(report["errors"])


if __name__ == "__main__":
    sys.exit(main())
