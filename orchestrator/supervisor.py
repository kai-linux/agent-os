"""Thin supervisor for parallel queue workers.

Reads max_parallel_workers from config and spawns up to that many concurrent
queue.py worker processes. Per-repo locking inside queue.py ensures no two
workers access the same repository simultaneously.

Each worker is an independent process (no shared mutable state, no GIL
contention). Workers are identified by QUEUE_WORKER_ID env var in logs.
"""

import os
import subprocess
import sys
import time

from orchestrator.paths import load_config, runtime_paths
from orchestrator.queue import maybe_run_stall_watchdog, process_telegram_callbacks


def main():
    cfg = load_config()
    max_workers = int(cfg.get("max_parallel_workers", 1))
    paths = runtime_paths(cfg)
    INBOX = paths["INBOX"]
    PROCESSING = paths["PROCESSING"]

    python = sys.executable
    procs: list[tuple[str, subprocess.Popen]] = []
    worker_counter = 0
    last_callback_poll = 0.0

    print(f"[supervisor] Starting with max_parallel_workers={max_workers}")

    while True:
        now = time.time()
        if now - last_callback_poll >= 15:
            process_telegram_callbacks(cfg, paths)
            last_callback_poll = now
        maybe_run_stall_watchdog(cfg, paths, worker_id="supervisor", queue_summary_log=paths["QUEUE_SUMMARY_LOG"])

        # Reap finished workers
        procs = [(wid, p) for wid, p in procs if p.poll() is None]

        has_inbox = any(INBOX.glob("*.md"))
        has_processing = any(PROCESSING.glob("*.md"))

        # Spawn up to max_workers when inbox has tasks
        while len(procs) < max_workers and any(INBOX.glob("*.md")):
            wid = f"w{worker_counter}"
            worker_counter += 1
            env = {**os.environ, "QUEUE_WORKER_ID": wid}
            p = subprocess.Popen([python, "-m", "orchestrator.queue"], env=env)
            print(f"[supervisor] Spawned worker {wid} (pid={p.pid})")
            procs.append((wid, p))
            has_inbox = any(INBOX.glob("*.md"))

        # Exit when no workers running and no tasks remain
        if not procs and not has_inbox and not has_processing:
            print("[supervisor] No tasks remaining. Exiting.")
            break

        time.sleep(1)


if __name__ == "__main__":
    main()
