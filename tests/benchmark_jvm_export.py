"""Paired, bounded JAR export diagnostics; not a CI performance gate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import statistics
import subprocess
import sys
import time
from jvm_source_quality import source_quality

try:
    import psutil
except ImportError:
    psutil = None

ROOT = Path(__file__).resolve().parents[1]


def measure(command, folder, env, cpus, timeout):
    peak = 0 if psutil else None
    start = time.perf_counter()
    timed_out = False
    def pin():
        os.sched_setaffinity(0, cpus)
    with (folder / "stdout.log").open("wb") as stdout, (folder / "stderr.log").open("wb") as stderr:
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stdout, stderr=stderr,
                                 start_new_session=True, preexec_fn=pin if cpus else None)
        monitored = psutil.Process(child.pid) if psutil else None
        try:
            while child.poll() is None:
                if monitored:
                    try:
                        rss = 0
                        for process in [monitored, *monitored.children(recursive=True)]:
                            try:
                                rss += process.memory_info().rss
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                        peak = max(peak, rss)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                if time.perf_counter() - start > timeout:
                    timed_out = True
                    os.killpg(child.pid, signal.SIGKILL) if os.name == "posix" else child.kill()
                    break
                time.sleep(0.01)
            code = child.wait()
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL) if os.name == "posix" else child.kill()
                child.wait()
    return {"command": command, "seconds": time.perf_counter() - start,
            "sampled_peak_tree_rss_bytes": peak, "returncode": code, "timed_out": timed_out}


def outputs(folder, tool):
    source = folder / "export"
    files = sorted(source.rglob("*.java"))
    failures = 0
    structuring = 0
    for path in files:
        body = path.read_text(encoding="utf-8", errors="replace")
        quality = source_quality(body)
        failures += quality["failed_method_markers"]
        structuring += quality["structuring_markers"]
    records = []
    if tool != "jadx":
        records = [json.loads(line) for line in (source / "manifest.jsonl").read_text().splitlines()]
    return {"java_files": len(files), "failure_markers": failures, "structuring_markers": structuring,
            "manifest_errors": sum(record.get("status") != "ok" for record in records),
            "class_hashes": {record["origin"]: record.get("output_sha256")
                             for record in records if "class" in record}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="Identical JAR inputs for all tools.")
    parser.add_argument("--baseline", type=Path, help="Prior ASC checkout; otherwise use current --batch-size 1.")
    parser.add_argument("--cfr-jar", type=Path, required=True)
    parser.add_argument("--jadx", type=Path)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 3 or args.threads < 1 or not 1 <= args.batch_size <= 256 or args.timeout <= 0:
        parser.error("need samples >=3, positive threads/timeout and batch size 1..256")
    args.output = args.output.resolve()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output directory must be empty")
    args.output.mkdir(parents=True, exist_ok=True)
    cpus = sorted(os.sched_getaffinity(0))[:args.threads] if hasattr(os, "sched_getaffinity") else []
    tools = ["baseline", "optimized"] + (["jadx"] if args.jadx else [])
    env = dict(os.environ, ASC_CFR_JAR=str(args.cfr_jar.resolve()), PYTHONHASHSEED="0",
               JAVA_TOOL_OPTIONS=f"-XX:ActiveProcessorCount={args.threads}",
               JAVA_OPTS=f"-Xmx{512 * args.threads}m",
               JADX_CONFIG_DIR=str(args.output / "jadx-config"),
               JADX_CACHE_DIR=str(args.output / "jadx-cache"),
               JADX_TMP_DIR=str(args.output / "jadx-tmp"))
    report = {"python": sys.version, "platform": platform.platform(), "cpus": cpus,
              "java": subprocess.run(["java", "-version"], capture_output=True, text=True).stderr.strip(),
              "cfr_sha256": hashlib.sha256(args.cfr_jar.read_bytes()).hexdigest(),
              "baseline": str(args.baseline.resolve()) if args.baseline else "current batch-size 1",
              "jadx": str(args.jadx.resolve()) if args.jadx else None,
              "samples": args.samples, "threads": args.threads, "batch_size": args.batch_size,
              "method": "Excluded warmup then alternating fresh processes, warm filesystem caches; "
                        "10ms sampled tree RSS if psutil is available. No semantic equivalence/significance claim.",
              "workloads": [], "errors": []}
    def save():
        (args.output / "results.json").write_text(json.dumps(report, indent=2) + "\n")

    for number, input_path in enumerate(args.inputs):
        input_path = input_path.resolve()
        if input_path.suffix.lower() != ".jar" or not input_path.is_file():
            parser.error("this diagnostic requires existing JAR files")
        workload = {"input": str(input_path), "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                    "runs": []}
        report["workloads"].append(workload)
        for index in range(args.samples + 1):
            order = tools if index % 2 == 0 else list(reversed(tools))
            for tool in order:
                folder = args.output / f"input-{number}" / ("warmup" if index == 0 else f"pair-{index}") / tool
                folder.mkdir(parents=True)
                if tool == "jadx":
                    command = [str(args.jadx.resolve()), "--config", "none", "--no-res", "--log-level", "warn",
                               "-j", str(args.threads), "-ds", str(folder / "export"), str(input_path)]
                else:
                    checkout = args.baseline.resolve() if tool == "baseline" and args.baseline else ROOT
                    command = [sys.executable, str(checkout / "main.py"), "export", str(input_path),
                               "--no-resources", "--threads", str(args.threads), "-o", str(folder / "export")]
                    if tool == "optimized" or not args.baseline:
                        command += ["--batch-size", str(args.batch_size if tool == "optimized" else 1)]
                run = measure(command, folder, env, cpus, args.timeout)
                run.update(tool=tool, pair=index, warmup=index == 0,
                           logs=str(folder.relative_to(args.output)))
                try:
                    run["output"] = outputs(folder, tool)
                except (ValueError, OSError) as exc:
                    run["output"] = {"error": str(exc)}
                workload["runs"].append(run)
                if run["returncode"] or run["output"].get("error") or run["output"].get("failure_markers") or run["output"].get("manifest_errors"):
                    report["errors"].append(f"{input_path.name} {tool} pair {index}: incomplete/failed output")
                save()
                print(f"{input_path.name} {tool} pair={index} {run['seconds']:.3f}s "
                      f"exit={run['returncode']} java={run['output'].get('java_files')}", flush=True)
        summary = {}
        for tool in tools:
            runs = [run for run in workload["runs"] if run["tool"] == tool and not run["warmup"]]
            rss = [run["sampled_peak_tree_rss_bytes"] / 2**20 for run in runs
                   if run["sampled_peak_tree_rss_bytes"] is not None]
            summary[tool] = {"median_seconds": statistics.median(run["seconds"] for run in runs),
                             "median_peak_tree_rss_mib": statistics.median(rss) if rss else None,
                             "complete_without_checked_failures": all(
                                 run["returncode"] == 0 and run["output"].get("java_files", 0) > 0
                                 and not any(run["output"].get(key) for key in
                                             ("error", "failure_markers", "manifest_errors")) for run in runs)}
        for index in range(1, args.samples + 1):
            pair = {run["tool"]: run for run in workload["runs"] if run["pair"] == index}
            old = pair["baseline"]["output"].get("class_hashes", {})
            new = pair["optimized"]["output"].get("class_hashes", {})
            if not old or old.keys() != new.keys() or len(new) != pair["optimized"]["output"].get("java_files"):
                report["errors"].append(f"{input_path.name} pair {index}: class output sets differ")
            pair["optimized"]["matching_baseline_hashes"] = sum(old.get(origin) == digest for origin, digest in new.items())
        workload["summary"] = summary
        save()
        print(json.dumps({input_path.name: summary}), flush=True)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
