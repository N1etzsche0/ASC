"""Decompiler comment markers are diagnostics, not a Java compilation oracle."""
from collections import Counter
import re

FAILURE_MARKERS = (
    "Method not decompiled", "Could not decompile", "Decompilation failed",
    "This method has failed to decompile.",
)
STRUCTURING_MARKER = "Unable to fully structure code"

_HOST_STACK = re.compile(
    r"(?m)^([ \t]*\*     at org\.benf\.cfr\.reader\.Main\.main\(Main\.java:\d+\)\r?\n)"
    r"(?:[ \t]*\*     at (?:java\.base/)?(?:jdk\.internal\.reflect\.[^\r\n]+|"
    r"sun\.reflect\.[^\r\n]+|java\.lang\.reflect\.Method\.invoke\([^\r\n]+\))\r?\n)*"
    r"[ \t]*\*     at CfrWorker\.main\(CfrWorker\.java:\d+\)\r?\n(?=[ \t]*\*/)")


def worker_stack_comment_equivalent(before, after):
    """Allow only the host's extra frames at the end of a CFR stack comment.

    For benchmark classification only: source files and raw hashes are retained.
    This does not remove general comments or validate Java semantics.
    """
    left, left_count = _HOST_STACK.subn(r"\1", before)
    right, right_count = _HOST_STACK.subn(r"\1", after)
    return bool(left_count or right_count) and left == right


def source_quality(text):
    failures = {marker: text.count(marker) for marker in FAILURE_MARKERS}
    return {"failure_markers": failures, "failed_method_markers": sum(failures.values()),
            "structuring_markers": text.count(STRUCTURING_MARKER),
            "jadx_warning_markers": text.count("JADX WARN:"),
            "jadx_error_markers": text.count("JADX ERROR:")}


def directory_quality(root):
    markers = Counter()
    files = failed_files = structuring_files = structuring = 0
    jadx_warnings = jadx_errors = jadx_warning_files = jadx_error_files = 0
    for path in root.rglob("*.java"):
        result = source_quality(path.read_text(encoding="utf-8", errors="replace"))
        files += 1
        failed_files += result["failed_method_markers"] > 0
        structuring_files += result["structuring_markers"] > 0
        structuring += result["structuring_markers"]
        jadx_warnings += result["jadx_warning_markers"]
        jadx_errors += result["jadx_error_markers"]
        jadx_warning_files += result["jadx_warning_markers"] > 0
        jadx_error_files += result["jadx_error_markers"] > 0
        markers.update(result["failure_markers"])
    return {"java_files": files, "files_with_failure_markers": failed_files,
            "failure_markers": dict(markers), "failed_method_markers": sum(markers.values()),
            "files_with_structuring_markers": structuring_files, "structuring_markers": structuring,
            "jadx_warning_markers": jadx_warnings, "jadx_error_markers": jadx_errors,
            "files_with_jadx_warnings": jadx_warning_files, "files_with_jadx_errors": jadx_error_files}
