"""Available-memory and cgroup constraints for automatic CFR batch sizing."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from droidasc.jvm.memory import MIB, available_memory_bytes, batch_size_for_memory, select_batch_size
from droidasc.cli import _build_main_parser


class JvmMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        (self.proc / "self").mkdir(parents=True)
        (self.proc / "meminfo").write_text("MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\n")
        self.mount = self.root / "cgroup with space"
        self.mount.mkdir()

    def cgroup(self, kind="cgroup2", mount_root="/", group="/parent/leaf"):
        membership = "0::" if kind == "cgroup2" else "3:cpu,memory:"
        (self.proc / "self/cgroup").write_text(membership + group + "\n")
        point = str(self.mount).replace(" ", "\\040")
        options = "rw" if kind == "cgroup2" else "rw,memory"
        (self.proc / "self/mountinfo").write_text(
            f"30 20 0:30 {mount_root} {point} rw - {kind} cgroup {options}\n")

    @staticmethod
    def values(directory, **values):
        directory.mkdir(parents=True, exist_ok=True)
        for name, value in values.items():
            (directory / name).write_text(str(value))

    def test_uses_available_instead_of_total_and_keeps_zero(self):
        self.assertEqual(available_memory_bytes(self.proc), 8192 * MIB)
        (self.proc / "meminfo").write_text("MemTotal: 16777216 kB\nMemAvailable: 0 kB\n")
        self.assertEqual(available_memory_bytes(self.proc), 0)

    def test_older_kernel_reclaim_estimate_excludes_shared_pages(self):
        (self.proc / "meminfo").write_text(
            "MemFree: 1048576 kB\nCached: 2097152 kB\nBuffers: 262144 kB\n"
            "SReclaimable: 262144 kB\nShmem: 524288 kB\nBogus: unavailable\n")
        self.assertEqual(available_memory_bytes(self.proc), 3072 * MIB)

    def test_cgroup_v2_checks_ancestor_usage_and_escaped_mount(self):
        self.cgroup()
        self.values(self.mount / "parent/leaf", **{"memory.max": 2048 * MIB,
                    "memory.high": "max", "memory.current": 512 * MIB})
        self.values(self.mount / "parent", **{"memory.max": 4096 * MIB,
                    "memory.current": 3072 * MIB})
        self.assertEqual(available_memory_bytes(self.proc), 1024 * MIB)

    def test_cgroup_v2_high_and_overcommitted_limits(self):
        self.cgroup(group="/")
        self.values(self.mount, **{"memory.max": "max", "memory.high": 2048 * MIB,
                    "memory.current": 1024 * MIB})
        self.assertEqual(available_memory_bytes(self.proc), 1024 * MIB)
        (self.mount / "memory.current").write_text(str(3072 * MIB))
        self.assertEqual(available_memory_bytes(self.proc), 0)

    def test_cgroup_v1_mount_root_and_unlimited_sentinel(self):
        self.cgroup("cgroup", mount_root="/tenant", group="/tenant/job")
        self.values(self.mount, **{"memory.limit_in_bytes": (1 << 63) - 4096,
                    "memory.usage_in_bytes": 4096 * MIB})
        self.values(self.mount / "job", **{"memory.limit_in_bytes": 2048 * MIB,
                    "memory.usage_in_bytes": 1536 * MIB})
        self.assertEqual(available_memory_bytes(self.proc), 512 * MIB)

    def test_private_namespace_maps_root_to_cgroup_mount(self):
        self.cgroup(mount_root="/tenant/job", group="/")
        self.values(self.mount, **{"memory.max": 1024 * MIB, "memory.current": 256 * MIB})
        self.assertEqual(available_memory_bytes(self.proc), 768 * MIB)

    def test_malformed_or_inaccessible_metadata_has_bounded_fallback(self):
        self.cgroup(group="/../outside")
        self.assertEqual(available_memory_bytes(self.proc), 8192 * MIB)
        (self.proc / "meminfo").unlink()
        with patch("droidasc.jvm.memory.os.sysconf", side_effect=ValueError("unavailable")):
            self.assertIsNone(available_memory_bytes(self.proc))
        with patch("droidasc.jvm.memory.os.sysconf", side_effect=[100, 4096]):
            self.assertEqual(available_memory_bytes(self.proc), 409600)

    def test_budget_reserves_headroom_and_scales_with_workers(self):
        for available, workers, expected in ((4096, 2, 64), (3072, 2, 32), (2048, 2, 16),
                                             (1280, 1, 8), (512, 2, 1), (0, 2, 1),
                                             (2048, 1, 64), (4096, 4, 16), (65536, 2, 64)):
            with self.subTest(available=available, workers=workers):
                self.assertEqual(batch_size_for_memory(available * MIB, workers), expected)
        self.assertEqual(batch_size_for_memory(None, 2), 16)
        with self.assertRaises(ValueError):
            batch_size_for_memory(4096 * MIB, 0)

    def test_explicit_value_bypasses_memory_probe_and_invalid_values_fail(self):
        with patch("droidasc.jvm.memory.available_memory_bytes", return_value=4096 * MIB) as read:
            self.assertEqual(select_batch_size(2)["batch_size"], 64)
            read.reset_mock()
            for size in (1, 16, 256):
                self.assertEqual(select_batch_size(2, size), {"batch_size": size, "batch_size_mode": "explicit"})
            read.assert_not_called()
            for workers, size in ((0, None), (2, 0), (2, 257)):
                with self.assertRaises(ValueError):
                    select_batch_size(workers, size)

    def test_cli_defaults_to_auto_and_accepts_numeric_override(self):
        parser = _build_main_parser()
        self.assertIsNone(parser.parse_args(["export", "app.jar", "-o", "out"]).batch_size)
        self.assertEqual(parser.parse_args(["export", "app.jar", "-o", "out", "--batch-size", "256"]).batch_size, 256)


if __name__ == "__main__":
    unittest.main()
