"""Fresh-process ASC/JADX full-JAR diagnostics with explicit resource budgets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from benchmark_jvm_export import ROOT, measure
from benchmark_jvm_large import inspect_outputs
from jvm_source_quality import directory_quality


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--tool", choices=("asc", "jadx"), required=True)
    parser.add_argument("--cfr-jar", type=Path)
    parser.add_argument("--jadx", type=Path)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--heap-mb", type=int, default=2048)
    parser.add_argument("--oom-heap-mb", type=int, default=8192)
    parser.add_argument("--jadx-heap-mb", type=int, default=16384)
    parser.add_argument("--zip-max-entries", type=int, default=200000)
    parser.add_argument("--run-timeout", type=float, default=3600)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.threads, args.run_timeout, args.jadx_heap_mb, args.zip_max_entries) <= 0:
        parser.error("resource limits must be positive")
    if ((args.tool == "asc" and not args.cfr_jar) or
            (args.tool == "jadx" and not args.jadx)):
        parser.error("supply the selected backend")
    if args.tool == "asc" and not 64 <= args.heap_mb < args.oom_heap_mb <= 8192:
        parser.error("ASC heaps must satisfy 64 <= heap-mb < oom-heap-mb <= 8192")
    if not args.input.is_file() or args.input.suffix.lower() != ".jar":
        parser.error("input must be an existing JAR")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output directory must be empty")
    folder = args.output.absolute()
    folder.mkdir(parents=True, exist_ok=True)
    cpus = sorted(os.sched_getaffinity(0))[:args.threads] if hasattr(os, "sched_getaffinity") else []
    env = dict(os.environ, PYTHONHASHSEED="0")
    if args.tool == "asc":
        env["JAVA_TOOL_OPTIONS"] = "-XX:ActiveProcessorCount=1"
        command = [sys.executable, str(ROOT / "main.py"), "export", str(args.input.resolve()),
                   "--cfr-jar", str(args.cfr_jar.resolve()), "--threads", str(args.threads),
                   "--heap-mb", str(args.heap_mb), "--oom-heap-mb", str(args.oom_heap_mb),
                   "--no-enum-switch", "--no-resources", "-o", str(folder / "export")]
        backend = {"cfr_sha256": hashlib.sha256(args.cfr_jar.read_bytes()).hexdigest(),
                   "heap_mb_per_worker": args.heap_mb, "oom_heap_mb": args.oom_heap_mb,
                   "maximum_concurrent_heap_mb": max(args.threads * args.heap_mb, args.oom_heap_mb)}
    else:
        env.update(JAVA_TOOL_OPTIONS=f"-XX:ActiveProcessorCount={args.threads}",
                   JAVA_OPTS=f"-Xmx{args.jadx_heap_mb}m",
                   JADX_ZIP_MAX_ENTRIES_COUNT=str(args.zip_max_entries),
                   JADX_CONFIG_DIR=str(folder / "config"), JADX_CACHE_DIR=str(folder / "cache"),
                   JADX_TMP_DIR=str(folder / "tmp"))
        command = [str(args.jadx.resolve()), "--config", "none", "--no-res", "--log-level", "warn",
                   "-j", str(args.threads), "-ds", str(folder / "export"), str(args.input.resolve())]
        backend = {"jadx_version": subprocess.run([str(args.jadx.resolve()), "--version"],
                    capture_output=True, text=True, check=True).stdout.strip(),
                   "maximum_concurrent_heap_mb": args.jadx_heap_mb,
                   "zip_max_entries": args.zip_max_entries}
    report = {"tool": args.tool, "input": str(args.input.resolve()),
              "input_bytes": args.input.stat().st_size,
              "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
              "python": sys.version, "platform": platform.platform(), "cpus": cpus,
              "threads": args.threads, "backend": backend, "samples": 1,
              "java": subprocess.run(["java", "-version"], capture_output=True, text=True).stderr.strip(),
              "asc_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.strip(),
              "method": "One fresh process, no excluded warmup; warm filesystem cache. "
                        "10 ms sampled sum of process-tree RSS; shared pages may be counted twice. "
                        "Run tools sequentially on the same CPU set. Different source layouts and "
                        "decompilation algorithms; no semantic-equivalence or significance claim."}
    (folder / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    report["run"] = measure(command, folder, env, cpus, args.run_timeout)
    report["source_quality"] = directory_quality(folder / "export")
    if args.tool == "asc":
        try:
            report["outputs"] = inspect_outputs(folder)
            report["summary"] = json.loads((folder / "stderr.log").read_text().splitlines()[-1])
        except (OSError, ValueError, KeyError, IndexError) as exc:
            report["inspection_error"] = str(exc)
    (folder / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)
    # Preserve the actual backend status; marker diagnostics are reported separately.
    return bool(report["run"]["returncode"] or report["run"]["timed_out"]
                or report.get("inspection_error")
                or report.get("outputs", {}).get("hash_mismatches"))


if __name__ == "__main__":
    sys.exit(main())
