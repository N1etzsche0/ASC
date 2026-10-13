"""Conservative export batch sizing without an optional runtime dependency."""
import os
from pathlib import Path, PurePosixPath
import re

MIB = 1024 * 1024


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _number(path):
    try:
        value = int(_read(path).strip())
        return value if value >= 0 else None
    except ValueError:
        return None


def _unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _cgroup_available(proc_root):
    """Account for enclosing v1/v2 limits, not just the current leaf group."""
    groups = {}
    for line in _read(proc_root / "self/cgroup").splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3:
            if not fields[1]:
                groups["cgroup2"] = fields[2]
            elif "memory" in fields[1].split(","):
                groups["cgroup"] = fields[2]
    budgets = []
    for line in _read(proc_root / "self/mountinfo").splitlines():
        before, separator, after = line.partition(" - ")
        left, right = before.split(), after.split()
        if not separator or len(left) < 5 or len(right) < 3:
            continue
        kind = right[0]
        if kind not in groups or (kind == "cgroup" and "memory" not in right[2].split(",")):
            continue
        mount_root = PurePosixPath(_unescape_mount(left[3]))
        group = PurePosixPath(groups[kind])
        if ".." in group.parts or not group.is_absolute() or not mount_root.is_absolute():
            continue
        try:
            # A private cgroup namespace can expose its mounted root as '/'.
            relative = PurePosixPath(".") if group == PurePosixPath("/") else group.relative_to(mount_root)
        except ValueError:
            continue
        boundary = Path(_unescape_mount(left[4]))
        if not boundary.is_absolute() or ".." in boundary.parts:
            continue
        directory = boundary / relative
        while True:
            usage_name = "memory.current" if kind == "cgroup2" else "memory.usage_in_bytes"
            usage = _number(directory / usage_name)
            names = ("memory.max", "memory.high") if kind == "cgroup2" else ("memory.limit_in_bytes",)
            for name in names:
                limit = _number(directory / name)
                # v1 represents 'unlimited' using a very large integer.
                if limit is not None and limit < 1 << 60 and usage is not None:
                    budgets.append(max(0, limit - usage))
            if directory == boundary:
                break
            directory = directory.parent
    return min(budgets) if budgets else None


def available_memory_bytes(proc_root=Path("/proc")):
    """Best-effort usable bytes, including remaining container memory."""
    proc_root = Path(proc_root)
    values = {}
    for line in _read(proc_root / "meminfo").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            try:
                values[key] = max(0, int(value.split()[0])) * 1024
            except (ValueError, IndexError):
                continue
    available = values.get("MemAvailable")
    if available is None and "MemFree" in values:
        available = max(0, sum(values.get(key, 0) for key in
                               ("MemFree", "Buffers", "Cached", "SReclaimable")) - values.get("Shmem", 0))
    if available is None:
        try:
            pages, page_size = os.sysconf("SC_AVPHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
            if pages >= 0 and page_size > 0:
                available = pages * page_size
        except (AttributeError, OSError, ValueError):
            pass
    if available is not None and values.get("MemTotal", 0) > 0:
        available = min(available, values["MemTotal"])
    container = _cgroup_available(proc_root)
    if container is not None:
        available = min(available, container) if available is not None else container
    return available


def batch_size_for_memory(available, workers):
    """Reserve 25% plus 512 MiB for inventory/other work, then divide by workers."""
    if workers <= 0:
        raise ValueError("Export worker count must be positive")
    if available is None:
        return 16
    budget = max(0, available * 3 // 4 - 512 * MIB) // workers
    for minimum, size in ((1024, 64), (768, 32), (512, 16), (384, 8)):
        if budget >= minimum * MIB:
            return size
    return 1


def select_batch_size(workers, batch_size=None):
    if workers <= 0 or (batch_size is not None and not 1 <= batch_size <= 256):
        raise ValueError("Export worker count must be positive and batch size must be 1..256")
    if batch_size is not None:
        return {"batch_size": batch_size, "batch_size_mode": "explicit"}
    available = available_memory_bytes()
    return {"batch_size": batch_size_for_memory(available, workers),
            "batch_size_mode": "auto", "available_memory_bytes": available}
