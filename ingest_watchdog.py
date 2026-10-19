"""Watchdog for LightRAG bulk ingest — detects deadlocks and auto-restarts.

Monitors heartbeat + log activity + CPU usage. Detects deadlocks:
- No new log output AND heartbeat stale for 10+ min
- CPU near 0% confirms deadlock (vs ONNX embedding which runs at high CPU)

On deadlock: kills process, cleans HF locks, resets stuck PG docs, restarts.
Progress file (bulk_ingest.done) ensures no re-processing after restart.

Usage: python -u ingest_watchdog.py
   or: start_watchdog.bat
"""
import subprocess
import time
import os
import json
import asyncio
import logging
import sys
import glob
import io
import psutil

# UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

MEMORY_DIR = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT_FILE = os.path.join(MEMORY_DIR, "bulk_ingest.heartbeat")
LOG_FILE = os.path.join(MEMORY_DIR, "bulk_ingest.log")
WATCHDOG_LOG = os.path.join(MEMORY_DIR, "watchdog.log")
HF_LOCKS_DIR = os.path.join(
    os.path.expanduser("~"),
    ".cache", "huggingface", "hub", ".locks",
    "models--onnx-community--embeddinggemma-300m-ONNX",
)
PYTHON_EXE = os.environ.get("MEMORY_PYTHON", sys.executable)
INGEST_SCRIPT = os.path.join(MEMORY_DIR, "run_bulk_ingest.py")

# Timing
POLL_INTERVAL = 30        # Check every 30s
STALE_THRESHOLD = 300     # 5 min no activity → suspicious
CPU_SAMPLES = 3           # Sample CPU 3 times (3s each = 9s total)
CPU_THRESHOLD = 5.0       # Below 5% = likely deadlocked (not ONNX)
RESTART_DELAY = 30        # Pause between restarts
MAX_RESTARTS = 100        # Safety limit


def setup_logging():
    fmt = "%(asctime)s [WATCHDOG] %(message)s"
    handlers = [
        logging.FileHandler(WATCHDOG_LOG, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)
    return logging.getLogger("watchdog")


def read_heartbeat():
    try:
        with open(HEARTBEAT_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def is_complete():
    hb = read_heartbeat()
    return hb and hb.get("phase") == "complete"


def log_file_size():
    try:
        return os.path.getsize(LOG_FILE)
    except Exception:
        return 0


def check_cpu(pid, log):
    """Check if process is using CPU (ONNX work) or near-zero (deadlock).

    Returns avg CPU% over ~9 seconds, or -1 if check fails.
    """
    try:
        proc = psutil.Process(pid)
        samples = []
        for _ in range(CPU_SAMPLES):
            cpu = proc.cpu_percent(interval=3)
            samples.append(cpu)
        avg = sum(samples) / len(samples)
        log.info(f"CPU samples PID {pid}: {[f'{s:.0f}%' for s in samples]} → avg {avg:.1f}%")
        return avg
    except psutil.NoSuchProcess:
        log.warning(f"PID {pid} no longer exists")
        return 0
    except Exception as e:
        log.warning(f"CPU check failed: {e}")
        return -1


def kill_zombie_processes(log, exclude_pid=None):
    """Kill any leftover run_bulk_ingest.py processes."""
    killed = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["name"] != "python.exe":
                continue
            cmdline = " ".join(proc.info["cmdline"] or [])
            if "run_bulk_ingest" in cmdline and "watchdog" not in cmdline:
                if exclude_pid and proc.info["pid"] == exclude_pid:
                    continue
                pid = proc.info["pid"]
                log.warning(f"Killing leftover ingest PID {pid}")
                proc.kill()
                killed.append(pid)
                time.sleep(1)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return killed


def clean_hf_locks(log):
    """Remove stale HuggingFace model lock files that prevent ONNX load."""
    if os.path.isdir(HF_LOCKS_DIR):
        locks = glob.glob(os.path.join(HF_LOCKS_DIR, "*.lock"))
        for lock in locks:
            try:
                os.remove(lock)
                log.info(f"Removed HF lock: {os.path.basename(lock)}")
            except Exception:
                pass


async def reset_stuck_docs(log):
    """Reset recently-stuck PROCESSING docs back to PENDING.

    Only resets docs stuck for <2 hours. Docs stuck longer are likely
    too large and would deadlock again — leave them alone.
    Does NOT touch 'failed' docs (may be force-completed or duplicates).
    """
    try:
        import asyncpg
        conn = await asyncpg.connect(os.environ["AGENT_MEMORY_DB_URI"])
        result = await conn.execute("""
            UPDATE lightrag_doc_status
            SET status = 'pending'
            WHERE status = 'processing'
            AND updated_at > NOW() - INTERVAL '2 hours'
        """)
        pending = await conn.fetchval(
            "SELECT count(*) FROM lightrag_doc_status WHERE status = 'pending'"
        )
        processed = await conn.fetchval(
            "SELECT count(*) FROM lightrag_doc_status WHERE status = 'processed'"
        )
        await conn.close()
        log.info(f"Reset docs: {result}. Processed: {processed}, Pending: {pending}")
    except Exception as e:
        log.error(f"Failed to reset docs: {e}")


def status_summary():
    """One-line status from heartbeat."""
    hb = read_heartbeat()
    if not hb:
        return "no heartbeat"
    return (
        f"phase={hb.get('phase')} "
        f"done={hb.get('done')}/{hb.get('total')} "
        f"errors={hb.get('errors')} "
        f"paper={str(hb.get('current_paper', '?'))[:50]}"
    )


def main():
    log = setup_logging()
    log.info("=" * 60)
    log.info("WATCHDOG STARTED")
    log.info(f"Stale threshold: {STALE_THRESHOLD}s, CPU threshold: {CPU_THRESHOLD}%")
    log.info(f"Max restarts: {MAX_RESTARTS}")
    log.info("=" * 60)

    # Ensure env vars
    if "ANTHROPIC_API_KEY" not in os.environ:
        print("Note: ANTHROPIC_API_KEY not set; the Haiku fallback will be unavailable", file=sys.stderr)

    restarts = 0

    while restarts < MAX_RESTARTS:
        # Already done?
        if is_complete():
            log.info("Ingest already complete. Nothing to do.")
            break

        # Pre-launch cleanup
        killed = kill_zombie_processes(log)
        if killed:
            time.sleep(5)
        clean_hf_locks(log)
        asyncio.run(reset_stuck_docs(log))

        # Launch
        restarts += 1
        log.info(f"--- LAUNCH #{restarts} ---")
        log.info(f"Status before launch: {status_summary()}")

        proc = subprocess.Popen(
            [PYTHON_EXE, "-u", INGEST_SCRIPT],
            cwd=MEMORY_DIR,
            # Inherits parent console — required for asyncio worker pool
        )
        log.info(f"Ingest started, PID {proc.pid}")

        # Monitor loop
        last_log_size = log_file_size()
        last_activity = time.time()

        while proc.poll() is None:
            time.sleep(POLL_INTERVAL)

            # Complete?
            if is_complete():
                log.info("Heartbeat says COMPLETE")
                proc.wait()
                break

            # Track activity via log size and heartbeat freshness
            current_log_size = log_file_size()
            hb = read_heartbeat()
            hb_age = (
                time.time() - hb["timestamp"]
                if hb and "timestamp" in hb
                else float("inf")
            )

            # Log file grew → active
            if current_log_size != last_log_size:
                last_log_size = current_log_size
                last_activity = time.time()

            # Fresh heartbeat → active
            if hb_age < POLL_INTERVAL * 2:
                last_activity = time.time()

            stale_seconds = time.time() - last_activity

            # Not stale yet → continue
            if stale_seconds <= STALE_THRESHOLD:
                continue

            # Stale! Check CPU to distinguish ONNX work from deadlock
            log.warning(
                f"STALE for {stale_seconds:.0f}s (heartbeat age: {hb_age:.0f}s). "
                f"Checking CPU..."
            )
            avg_cpu = check_cpu(proc.pid, log)

            if avg_cpu > CPU_THRESHOLD:
                # High CPU = ONNX embedding phase, not deadlock
                log.info(
                    f"CPU {avg_cpu:.1f}% > {CPU_THRESHOLD}% — "
                    f"ONNX embedding in progress, not a deadlock. Resetting timer."
                )
                last_activity = time.time()
                continue

            if avg_cpu < 0:
                # CPU check failed — wait 5 more minutes as safety
                log.warning("CPU check inconclusive. Waiting 5 more minutes...")
                time.sleep(300)
                if log_file_size() != last_log_size or proc.poll() is not None:
                    log.info("Activity resumed or process exited during extended wait.")
                    last_log_size = log_file_size()
                    last_activity = time.time()
                    continue

            # Low CPU + stale = deadlock confirmed
            log.error(
                f"DEADLOCK CONFIRMED: CPU={avg_cpu:.1f}%, "
                f"stale for {stale_seconds:.0f}s"
            )
            log.error(f"Killing PID {proc.pid}...")
            try:
                proc.kill()
                proc.wait(timeout=30)
            except Exception as e:
                log.error(f"Kill failed: {e}. Trying taskkill...")
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10,
                )
                time.sleep(5)
            log.info(f"Status at kill: {status_summary()}")
            break

        exit_code = proc.returncode
        log.info(f"Process exited, code={exit_code}")

        # Check final state
        if is_complete():
            hb = read_heartbeat()
            log.info("=" * 60)
            log.info("ALL PAPERS INGESTED")
            if hb:
                log.info(f"  Done:   {hb.get('done')}")
                log.info(f"  Errors: {hb.get('errors')}")
                log.info(f"  Time:   {hb.get('elapsed_min')} min")
                if hb.get("error_ids"):
                    log.info(f"  Failed: {hb.get('error_ids')}")
            log.info("=" * 60)
            break

        log.info(f"Restarting in {RESTART_DELAY}s...")
        time.sleep(RESTART_DELAY)

    if restarts >= MAX_RESTARTS:
        log.error(f"Hit MAX_RESTARTS ({MAX_RESTARTS}). Manual intervention needed.")

    log.info("WATCHDOG EXITING")


if __name__ == "__main__":
    main()
