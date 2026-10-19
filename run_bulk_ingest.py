"""Bulk ingest launcher — MUST run in foreground (not nohup).

nohup/background processes on Windows deadlock LightRAG's asyncio worker pool.
Run from a terminal: python -u run_bulk_ingest.py
Or from Claude Code: ! cd ~/.claude/memory && python -u run_bulk_ingest.py

Logs to bulk_ingest.log AND stdout.
Writes heartbeat to bulk_ingest.heartbeat (JSON) for monitoring.
"""
import asyncio
import sys
import os
import io
import time
import json
import logging

sys.path.insert(0, os.path.dirname(__file__))

# UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

LOG_FILE = os.path.join(os.path.dirname(__file__), "bulk_ingest.log")
HEARTBEAT_FILE = os.path.join(os.path.dirname(__file__), "bulk_ingest.heartbeat")
LOCK_FILE = os.path.join(os.path.dirname(__file__), "bulk_ingest.lock")
TASK_DUMP_FILE = os.path.join(os.path.dirname(__file__), "task_dump.log")
PER_PAPER_TIMEOUT = 14400  # 4 hours max per paper — big textbooks (1000+ chunks) need extended time


async def watchdog_dumper(interval: int = 60):
    """Periodically dump all live asyncio task stacks AND keyed-lock registry
    state to TASK_DUMP_FILE.

    Diagnostic-only. Reveals exactly where tasks are suspended when the
    merge_nodes_and_edges path hangs inside its semaphore+keyed_lock block.
    """
    import io
    import traceback
    # Short first delay so the first snapshot lands during the first batch
    await asyncio.sleep(30)
    while True:
        try:
            buf = io.StringIO()
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            tasks = [t for t in asyncio.all_tasks() if not t.done()]
            buf.write(f"\n{'='*78}\n=== TASK DUMP @ {now} — {len(tasks)} live tasks ===\n{'='*78}\n")
            for i, t in enumerate(tasks):
                name = t.get_name()
                coro = t.get_coro()
                coro_name = getattr(coro, "__qualname__", repr(coro))
                buf.write(f"\n--- [{i}] Task name={name} coro={coro_name} ---\n")
                try:
                    t.print_stack(file=buf)
                except Exception as e:
                    buf.write(f"  (print_stack failed: {e})\n")

            # --- Dump keyed-lock registry state ---
            try:
                from lightrag.kg import shared_storage as _ss
                mgr = getattr(_ss, "_storage_keyed_lock", None)
                if mgr is not None:
                    buf.write(f"\n--- KEYED LOCK REGISTRY ---\n")
                    counts = dict(mgr._async_lock_count)
                    cleanup = dict(mgr._async_lock_cleanup_data)
                    locks = mgr._async_lock
                    positive = {k: v for k, v in counts.items() if v > 0}
                    buf.write(f"total_keys={len(counts)}  positive_count={len(positive)}  pending_cleanup={len(cleanup)}\n")
                    # Show any key with positive ref count AND the actual lock.locked() state
                    for k, c in sorted(positive.items(), key=lambda kv: -kv[1])[:30]:
                        lk = locks.get(k)
                        locked = lk.locked() if lk is not None else "no_lock_obj"
                        buf.write(f"  count={c:3d}  locked={locked}  key={k!r}\n")
                else:
                    buf.write("\n--- KEYED LOCK REGISTRY: not initialized ---\n")
            except Exception as e:
                buf.write(f"\n--- KEYED LOCK DUMP FAILED: {e} ---\n{traceback.format_exc()}\n")
            # --- End keyed-lock dump ---

            with open(TASK_DUMP_FILE, "a", encoding="utf-8") as f:
                f.write(buf.getvalue())
        except Exception as e:
            try:
                with open(TASK_DUMP_FILE, "a", encoding="utf-8") as f:
                    f.write(f"\n[watchdog error: {e}]\n{traceback.format_exc()}\n")
            except Exception:
                pass
        await asyncio.sleep(interval)


def acquire_lock():
    """Prevent concurrent runs. Returns True if lock acquired."""
    pid = os.getpid()
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            # Check if old process is still alive (Windows)
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {old_pid}", "/NH"],
                capture_output=True, text=True
            )
            if str(old_pid) in result.stdout:
                print(f"ERROR: Another bulk ingest is running (PID {old_pid}). Kill it first or delete {LOCK_FILE}")
                return False
            else:
                print(f"Stale lock from dead process {old_pid} — overwriting")
        except (ValueError, OSError):
            print("Stale lock file — overwriting")
    with open(LOCK_FILE, "w") as f:
        f.write(str(pid))
    return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def setup_logging():
    fmt = "%(asctime)s %(message)s"
    handlers = [
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    # force=True ensures root logger is reconfigured even if already set up by imports
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)
    # Explicitly capture LightRAG's logger so merge/extraction messages appear
    for name in ("lightrag", "lightrag.utils", "lightrag.operate", "lightrag_kb"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.handlers = handlers
        lg.propagate = False
    return logging.getLogger("bulk_ingest")


def write_heartbeat(state: dict):
    """Write current state to heartbeat file for external monitoring."""
    state["timestamp"] = time.time()
    state["timestamp_human"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass  # Non-critical


async def run_all():
    log = setup_logging()
    log.info("=== BULK INGEST START ===")
    start = time.time()

    # Start watchdog: dumps asyncio task stacks every 60s to task_dump.log
    # Diagnostic for the merge_nodes_and_edges hang investigation.
    try:
        os.remove(TASK_DUMP_FILE)
    except OSError:
        pass
    watchdog_task = asyncio.create_task(watchdog_dumper(interval=60))
    log.info(f"Watchdog dumper started, writing to {TASK_DUMP_FILE}")

    write_heartbeat({"phase": "initializing", "done": 0, "errors": 0, "total": 0})

    # Preload ONNX embedding model before any async work
    log.info("Preloading embedding model...")
    from lightrag_kb import _get_embed_model
    _get_embed_model()
    log.info("Embedding model ready")

    # Initialize LightRAG (applies all patches)
    from lightrag_kb import get_rag, ingest_paper_from_db, reset_rag, CHUNKS_PER_VIRTUAL_BOOK
    rag = await get_rag()
    log.info("LightRAG initialized with quality filters")

    # Clean up orphaned/stuck entries from prior failed runs
    import asyncpg
    conn = await asyncpg.connect(os.environ["AGENT_MEMORY_DB_URI"])

    # 1. Delete dup-* records (duplicate detection artifacts)
    dup_count = await conn.fetchval(
        "SELECT count(*) FROM lightrag_doc_status WHERE id LIKE 'dup-%'"
    )
    if dup_count:
        await conn.execute("DELETE FROM lightrag_doc_status WHERE id LIKE 'dup-%'")
        log.info(f"Deleted {dup_count} dup-* records from doc_status")

    # 2. Delete non-processed doc_status entries
    stuck_count = await conn.fetchval(
        "SELECT count(*) FROM lightrag_doc_status WHERE status NOT IN ('processed')"
    )
    if stuck_count > 0:
        await conn.execute(
            "DELETE FROM lightrag_doc_status WHERE status NOT IN ('processed')"
        )
        log.info(f"Deleted {stuck_count} non-processed doc_status entries")

    # 3. Delete orphaned full_docs (content with no matching processed status)
    orphan_count = await conn.fetchval("""
        SELECT count(*) FROM lightrag_doc_full f
        WHERE NOT EXISTS (
            SELECT 1 FROM lightrag_doc_status s
            WHERE s.id = f.id AND s.workspace = f.workspace AND s.status = 'processed'
        )
    """)
    if orphan_count > 0:
        await conn.execute("""
            DELETE FROM lightrag_doc_full f
            WHERE NOT EXISTS (
                SELECT 1 FROM lightrag_doc_status s
                WHERE s.id = f.id AND s.workspace = f.workspace AND s.status = 'processed'
            )
        """)
        log.info(f"Deleted {orphan_count} orphaned full_docs entries")
    else:
        log.info("No orphaned docs to clear")

    # Verify consistency
    status_count = await conn.fetchval(
        "SELECT count(*) FROM lightrag_doc_status WHERE status = 'processed'"
    )
    full_count = await conn.fetchval("SELECT count(*) FROM lightrag_doc_full")
    log.info(f"Doc store consistency: {status_count} processed status, {full_count} full_docs")

    await conn.close()
    PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "bulk_ingest.done")
    PARTS_FILE = os.path.join(os.path.dirname(__file__), "bulk_ingest_parts.done")
    FAILED_FILE = os.path.join(os.path.dirname(__file__), "bulk_ingest_parts.failed")
    MAX_RETRIES = 2  # Skip parts that have failed this many times
    done_ids = set()
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            done_ids = {int(line.strip()) for line in f if line.strip().isdigit()}

    # Track completed virtual book parts: "paper_id:offset" format
    done_parts = set()
    if os.path.exists(PARTS_FILE):
        with open(PARTS_FILE) as f:
            done_parts = {line.strip() for line in f if ":" in line.strip()}

    # Track failure counts: "paper_id:offset" → count
    fail_counts = {}
    if os.path.exists(FAILED_FILE):
        with open(FAILED_FILE) as f:
            for line in f:
                key = line.strip()
                if ":" in key:
                    fail_counts[key] = fail_counts.get(key, 0) + 1
    permanently_skipped = {k for k, v in fail_counts.items() if v >= MAX_RETRIES}
    if permanently_skipped:
        log.info(f"Skipping {len(permanently_skipped)} parts with {MAX_RETRIES}+ failures: {permanently_skipped}")

    conn = await asyncpg.connect(os.environ["AGENT_MEMORY_DB_URI"])
    # Process smallest papers first — big books last (avoids blocking on huge docs)
    all_papers = await conn.fetch("""
        SELECT p.id, p.title, count(pc.id) as chunks
        FROM papers p
        LEFT JOIN paper_chunks pc ON pc.paper_id = p.id
        GROUP BY p.id, p.title
        ORDER BY chunks ASC
    """)
    await conn.close()
    papers = [p for p in all_papers if p["id"] not in done_ids]

    # Expand large papers into virtual books — each virtual book is processed
    # and reset exactly like a small paper, preventing lock accumulation
    work_items = []  # list of (paper_id, title, chunk_offset, chunk_limit, total_chunks)
    for p in papers:
        pid = p["id"]
        title = p["title"][:70] if p["title"] else f"paper_{pid}"
        n_chunks = p["chunks"]
        if n_chunks <= CHUNKS_PER_VIRTUAL_BOOK:
            # Small paper — process whole thing
            work_items.append((pid, title, 0, None, n_chunks))
        else:
            # Large book — split into virtual books
            for offset in range(0, n_chunks, CHUNKS_PER_VIRTUAL_BOOK):
                part_key = f"{pid}:{offset}"
                if part_key in done_parts:
                    continue  # Already completed this part
                if part_key in permanently_skipped:
                    continue  # Too many failures — skip
                limit = min(CHUNKS_PER_VIRTUAL_BOOK, n_chunks - offset)
                part_num = offset // CHUNKS_PER_VIRTUAL_BOOK + 1
                total_parts = (n_chunks + CHUNKS_PER_VIRTUAL_BOOK - 1) // CHUNKS_PER_VIRTUAL_BOOK
                part_title = f"{title} (Part {part_num}/{total_parts})"
                work_items.append((pid, part_title, offset, limit, n_chunks))

    log.info(f"{len(work_items)} work items ({len(done_ids)} papers done, {len(done_parts)} parts done)")

    done = 0
    skipped = 0
    errors = 0
    error_ids = []

    for i, (pid, title, chunk_offset, chunk_limit, total_chunks) in enumerate(work_items):
        log.info(f"[{i+1}/{len(work_items)}] {title} ({total_chunks} total chunks)")

        write_heartbeat({
            "phase": "ingesting",
            "current_paper": title,
            "current_index": i + 1,
            "total": len(work_items),
            "done": done,
            "skipped": skipped,
            "errors": errors,
            "elapsed_min": round((time.time() - start) / 60, 1),
        })

        try:
            await asyncio.wait_for(
                ingest_paper_from_db(pid, chunk_offset=chunk_offset, chunk_limit=chunk_limit),
                timeout=PER_PAPER_TIMEOUT,
            )
            done += 1
            if chunk_limit is None:
                # Small paper — whole thing done
                with open(PROGRESS_FILE, "a") as pf:
                    pf.write(f"{pid}\n")
            else:
                # Virtual book part — record part completion
                part_key = f"{pid}:{chunk_offset}"
                with open(PARTS_FILE, "a") as pf:
                    pf.write(f"{part_key}\n")
        except asyncio.TimeoutError:
            errors += 1
            error_ids.append(f"{pid}@{chunk_offset}")
            log.error(f"  TIMEOUT {title} after {PER_PAPER_TIMEOUT}s — skipping")
            if chunk_limit is not None:
                with open(FAILED_FILE, "a") as ff:
                    ff.write(f"{pid}:{chunk_offset}\n")
        except Exception as e:
            errors += 1
            error_ids.append(f"{pid}@{chunk_offset}")
            log.error(f"  ERROR {title}: {e}")
            if chunk_limit is not None:
                with open(FAILED_FILE, "a") as ff:
                    ff.write(f"{pid}:{chunk_offset}\n")

        # Reinitialize LightRAG between work items — clears internal keyed locks
        # Each virtual book gets a fresh start, just like small papers do
        try:
            await reset_rag()
        except Exception as e:
            log.warning(f"  Failed to reset LightRAG: {e}")

        # --- Zombie cleanup: delete non-processed docs before reinit ---
        # LightRAG's _validate_and_fix_document_consistency() resets
        # PROCESSING/FAILED docs to PENDING on every ainsert() call.
        # Clean them out so the next work item starts with a blank slate.
        try:
            cleanup_conn = await asyncpg.connect(os.environ["AGENT_MEMORY_DB_URI"])
            cleanup_del = await cleanup_conn.execute(
                "DELETE FROM lightrag_doc_status WHERE status NOT IN ('processed')"
            )
            orphan_del = await cleanup_conn.execute("""
                DELETE FROM lightrag_doc_full f
                WHERE NOT EXISTS (
                    SELECT 1 FROM lightrag_doc_status s
                    WHERE s.id = f.id AND s.workspace = f.workspace AND s.status = 'processed'
                )
            """)
            await cleanup_conn.close()
        except Exception as e:
            log.warning(f"  Inter-item cleanup failed: {e}")
        # --- End zombie cleanup ---

        try:
            rag = await get_rag()
            log.info("  LightRAG reinitialized")
        except Exception as e:
            log.warning(f"  Failed to reinitialize LightRAG: {e}")

        # Rate-limit pause (Gemini free tier: 15 RPM)
        if i < len(work_items) - 1:
            await asyncio.sleep(1)

        # Progress checkpoint every 25 items
        if (i + 1) % 25 == 0:
            elapsed = (time.time() - start) / 60
            rate = done / max(elapsed, 0.1)
            remaining = (len(work_items) - i - 1) / max(rate, 0.01)
            log.info(
                f"  CHECKPOINT: {done} done, {skipped} skipped, {errors} errors, "
                f"{elapsed:.0f}m elapsed, ~{remaining:.0f}m remaining"
            )

    elapsed = (time.time() - start) / 60
    log.info(f"=== BULK INGEST COMPLETE ===")
    log.info(f"  Done: {done}, Skipped: {skipped}, Errors: {errors}")
    log.info(f"  Time: {elapsed:.1f} min")
    if error_ids:
        log.info(f"  Failed IDs: {error_ids}")

    write_heartbeat({
        "phase": "complete",
        "done": done,
        "skipped": skipped,
        "errors": errors,
        "total": len(work_items),
        "elapsed_min": round(elapsed, 1),
        "error_ids": error_ids,
    })

    # Stop the watchdog cleanly
    try:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except (asyncio.CancelledError, Exception):
            pass
        log.info("Watchdog dumper stopped")
    except Exception:
        pass


if __name__ == "__main__":
    if not acquire_lock():
        sys.exit(1)
    try:
        asyncio.run(run_all())
    except Exception as e:
        # Capture crash to log file — Python tracebacks go to stderr only by default
        import traceback
        crash_msg = traceback.format_exc()
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n=== CRASH ===\n{crash_msg}\n")
        except Exception:
            pass
        print(f"\n=== CRASH ===\n{crash_msg}", file=sys.stderr)
        sys.exit(1)
    finally:
        release_lock()
