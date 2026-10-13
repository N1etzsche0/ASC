"""Export-scoped CFR JVMs. Requests retain CFR CLI's independent analysis state."""
from pathlib import Path
from queue import Queue, Empty
import shutil
import struct
import subprocess
import tempfile
from threading import Lock, Thread
import warnings

from .context import ContextMetadataCache

MAGIC = 0x41534331
MAX_TEXT = 65536


def _read_exact(stream, length):
    chunks = []
    while length:
        chunk = stream.read(length)
        if not chunk:
            raise ValueError("CFR worker closed its response pipe")
        chunks.append(chunk)
        length -= len(chunk)
    return b"".join(chunks)


class CfrWorkerPool:
    """Compile one bridge per export; own and reap every thread-local worker."""

    def __init__(self, mode="auto", small_export=False):
        if mode not in {"auto", "worker", "process"}:
            raise ValueError("CFR mode must be auto, worker or process")
        self.mode = mode
        self.small_export = small_export
        self.lock = Lock()
        self.workers = []
        self.temp = None
        self.unavailable = False
        self.compile_error = None
        self.context_cache = ContextMetadataCache()
        self.counters = {"cfr_worker_starts": 0, "cfr_worker_requests": 0,
                         "cfr_process_starts": 0, "cfr_worker_fallbacks": 0}

    def count(self, key):
        with self.lock:
            self.counters[key] += 1

    def worker(self):
        with self.lock:
            worker = CfrWorker(self)
            self.workers.append(worker)
            return worker

    def bridge(self, java):
        if self.mode == "process" or (self.mode == "auto" and self.small_export):
            return None
        with self.lock:
            if self.compile_error is not None:
                raise ValueError(self.compile_error)
            if self.unavailable:
                return None
            if self.temp is not None:
                return Path(self.temp.name)
            # Prefer a compiler belonging to the explicitly selected Java runtime.
            sibling = Path(java).resolve().with_name("javac.exe" if java.endswith(".exe") else "javac")
            compiler = str(sibling) if sibling.is_file() else shutil.which("javac")
            temp = tempfile.TemporaryDirectory(prefix="asc-cfr-worker-")
            try:
                if not compiler:
                    raise ValueError("CFR worker mode requires javac (a JDK); use --cfr-mode process with a JRE")
                # Java 8 bytecode also works with old Java runtimes and javac 8.
                result = subprocess.run([compiler, "-source", "8", "-target", "8", "-encoding", "UTF-8",
                                         "-d", temp.name, str(Path(__file__).with_name("CfrWorker.java"))],
                                        capture_output=True, text=True, timeout=60)
                if result.returncode:
                    raise ValueError("Cannot compile CFR worker: " + result.stderr[-4000:])
                self.temp = temp
                return Path(temp.name)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                temp.cleanup()
                if self.mode == "worker":
                    self.compile_error = str(exc)
                    raise ValueError(str(exc)) from exc
                self.unavailable = True
                self.counters["cfr_worker_fallbacks"] += 1
                warnings.warn(f"CFR worker unavailable; using process mode: {exc}", RuntimeWarning)
                return None

    def close_workers(self):
        # Caller drains active jobs first (export shutdown or exclusive heap retry).
        for worker in self.workers:
            worker.close()

    def close(self):
        self.close_workers()
        if self.temp is not None:
            self.temp.cleanup()
            self.temp = None
        self.context_cache.clear()


class CfrWorker:
    def __init__(self, pool):
        self.pool = pool
        self.process = None
        self.stderr = None
        self.command = None

    @property
    def context_cache(self):
        return self.pool.context_cache

    def close(self):
        process = self.process
        if process is not None:
            # Kill/reap rather than await a backend that may be stuck or exhausted.
            if process.poll() is None:
                process.kill()
            process.wait()
            for pipe in (process.stdin, process.stdout):
                pipe.close()
            self.process = None
        if self.stderr is not None:
            self.stderr.close()
            self.stderr = None

    def run(self, command, timeout):
        bridge = self.pool.bridge(command[0])
        if bridge is None:
            self.pool.count("cfr_process_starts")
            return subprocess.run(command, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        signature = tuple(command[:4])
        fresh = self.process is None or self.process.poll() is not None or self.command != signature
        if fresh:
            self.close()
            self.stderr = tempfile.TemporaryFile()
            import os
            try:
                self.process = subprocess.Popen([command[0], command[1], "-cp",
                    str(bridge) + os.pathsep + command[3], "CfrWorker"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr)
            except Exception:
                self.close()
                raise
            self.command = signature
            self.pool.count("cfr_worker_starts")
        self.pool.count("cfr_worker_requests")
        process = self.process
        replies = Queue(maxsize=1)

        def exchange():
            try:
                if fresh and struct.unpack(">i", _read_exact(process.stdout, 4))[0] != MAGIC:
                    raise ValueError("Invalid CFR worker handshake")
                args = [value.encode("utf-8") for value in command[4:]]
                if not 1 <= len(args) <= 1024 or any(len(arg) > MAX_TEXT for arg in args):
                    raise ValueError("CFR worker request exceeds protocol limits")
                payload = struct.pack(">i", len(args)) + b"".join(
                    struct.pack(">i", len(arg)) + arg for arg in args)
                process.stdin.write(payload)
                process.stdin.flush()
                status, heap, length = struct.unpack(">iii", _read_exact(process.stdout, 12))
                if status not in (0, 1) or heap not in (0, 1) or not 0 <= length <= MAX_TEXT:
                    raise ValueError("Invalid CFR worker response")
                diagnostics = _read_exact(process.stdout, length).decode("utf-8", errors="replace")
                if heap:
                    diagnostics = "java.lang.OutOfMemoryError: Java heap space\n" + diagnostics
                    status = 1
                replies.put(subprocess.CompletedProcess(command, status, "", diagnostics))
            except Exception as exc:
                replies.put(exc)

        reader = Thread(target=exchange, daemon=True)
        reader.start()
        try:
            result = replies.get(timeout=timeout)
        except Empty:
            self.close()
            reader.join(timeout=5)
            raise subprocess.TimeoutExpired(command, timeout) from None
        if isinstance(result, Exception):
            self.stderr.seek(0)
            diagnostic = self.stderr.read(MAX_TEXT).decode("utf-8", errors="replace")
            self.close()
            reader.join(timeout=5)
            return subprocess.CompletedProcess(command, 1, "", f"{result}\n{diagnostic}")
        reader.join()
        if result.returncode:
            self.close()
        return result
