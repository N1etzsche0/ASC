"""Reproducible multi-JAR scale probe; timings are diagnostic, not CI budgets."""
import argparse
import json
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from droidasc.jvm.application import JvmApplication


def benchmark(path, samples):
    timings = []
    for _ in range(samples):
        start = time.perf_counter()
        with JvmApplication(path) as app:
            inventory_end = time.perf_counter()
            classes = len(app.entries)
            hits = sum(1 for _ in app.findrefs("string", "ASC-BENCH-token"))
            end = time.perf_counter()
        timings.append({"inventory_seconds": inventory_end - start,
                        "scan_seconds": end - inventory_end,
                        "classes": classes, "hits": hits})
    return timings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jars", type=int, default=200)
    parser.add_argument("--classes-per-jar", type=int, default=50)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.jars, args.classes_per_jar, args.samples) <= 0:
        parser.error("counts must be positive")
    javac = shutil.which("javac")
    if javac is None:
        parser.error("JDK 17+ required")
    with tempfile.TemporaryDirectory(prefix="asc-jvm-bench-") as temp:
        temp = Path(temp)
        source = temp / "source"
        classes = temp / "classes"
        app = temp / "application"
        source.mkdir()
        app.mkdir()
        for index in range(args.classes_per_jar):
            (source / f"Item{index}.java").write_text(
                f'package bench; public class Item{index} {{ '
                'public String token() { return "ASC-BENCH-token"; } }', encoding="utf-8")
        subprocess.run([javac, "--release", "17", "-d", str(classes),
                        *map(str, sorted(source.glob("*.java")))], check=True, timeout=120)
        template = temp / "template.jar"
        with zipfile.ZipFile(template, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(classes.rglob("*.class")):
                archive.write(path, path.relative_to(classes).as_posix())
        for index in range(args.jars):
            shutil.copyfile(template, app / f"library-{index:05d}.jar")
        timings = benchmark(app, args.samples)
    expected = args.jars * args.classes_per_jar
    if any(sample["classes"] != expected or sample["hits"] != expected for sample in timings):
        raise SystemExit("Incomplete inventory/reference result")
    report = {"python": sys.version, "platform": platform.platform(),
              "jars": args.jars, "classes_per_jar": args.classes_per_jar,
              "expected_definitions": expected, "samples": timings,
              "median_inventory_seconds": statistics.median(s["inventory_seconds"] for s in timings),
              "median_scan_seconds": statistics.median(s["scan_seconds"] for s in timings)}
    try:
        import resource
        report["peak_process_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == "darwin" else 1024)
    except ImportError:
        pass
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
