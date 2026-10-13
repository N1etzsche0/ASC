"""Decompiler comment markers are diagnostics, not a Java compilation oracle."""
from collections import Counter

FAILURE_MARKERS = (
    "Method not decompiled", "Could not decompile", "Decompilation failed",
    "This method has failed to decompile.",
)
STRUCTURING_MARKER = "Unable to fully structure code"


def source_quality(text):
    failures = {marker: text.count(marker) for marker in FAILURE_MARKERS}
    return {"failure_markers": failures, "failed_method_markers": sum(failures.values()),
            "structuring_markers": text.count(STRUCTURING_MARKER)}


def directory_quality(root):
    markers = Counter()
    files = failed_files = structuring_files = structuring = 0
    for path in root.rglob("*.java"):
        result = source_quality(path.read_text(encoding="utf-8", errors="replace"))
        files += 1
        failed_files += result["failed_method_markers"] > 0
        structuring_files += result["structuring_markers"] > 0
        structuring += result["structuring_markers"]
        markers.update(result["failure_markers"])
    return {"java_files": files, "files_with_failure_markers": failed_files,
            "failure_markers": dict(markers), "failed_method_markers": sum(markers.values()),
            "files_with_structuring_markers": structuring_files, "structuring_markers": structuring}
