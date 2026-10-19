"""Quick ingest status checker — run from any terminal or Claude session.

Usage: python check_ingest_status.py
"""
import os
import json
import time
import psycopg2

HEARTBEAT_FILE = os.path.join(os.path.dirname(__file__), "bulk_ingest.heartbeat")
LOG_FILE = os.path.join(os.path.dirname(__file__), "bulk_ingest.log")


def check():
    # 1. Heartbeat file
    print("=== INGEST STATUS ===\n")

    if os.path.exists(HEARTBEAT_FILE):
        with open(HEARTBEAT_FILE) as f:
            hb = json.load(f)
        age = time.time() - hb.get("timestamp", 0)
        age_str = f"{age:.0f}s ago" if age < 120 else f"{age/60:.0f}m ago"
        alive = age < 900  # 15 min — large books can take 10+ min per paper

        print(f"Heartbeat: {hb.get('timestamp_human', '?')} ({age_str})")
        print(f"Phase:     {hb.get('phase', '?')}")
        print(f"Progress:  {hb.get('done', '?')}/{hb.get('total', '?')} done, "
              f"{hb.get('errors', 0)} errors, {hb.get('skipped', 0)} skipped")
        if hb.get("current_paper"):
            print(f"Current:   [{hb.get('current_index')}/{hb.get('total')}] {hb['current_paper']}")
        print(f"Elapsed:   {hb.get('elapsed_min', '?')} min")
        print(f"Process:   {'ALIVE' if alive else 'DEAD (stale heartbeat)'}")
    else:
        print("Heartbeat: No heartbeat file — ingest never started or file was deleted")

    # 2. DB status
    print()
    try:
        conn = psycopg2.connect(os.environ["AGENT_MEMORY_DB_URI"])
        cur = conn.cursor()

        cur.execute("SELECT count(*) FROM papers")
        total_papers = cur.fetchone()[0]

        cur.execute("SELECT status, count(*) FROM lightrag_doc_status GROUP BY status ORDER BY status")
        statuses = dict(cur.fetchall())

        cur.execute("SELECT count(*) FROM lightrag_entity_chunks")
        entities = cur.fetchone()[0]

        cur.execute("SELECT count(*) FROM lightrag_relation_chunks")
        relations = cur.fetchone()[0]

        tracked = sum(statuses.values())
        processed = statuses.get("processed", 0)
        processing = statuses.get("processing", 0)
        failed = statuses.get("failed", 0)
        pending = statuses.get("pending", 0)

        print(f"Papers:    {total_papers} total")
        print(f"Tracked:   {tracked} in doc_status")
        print(f"  processed:  {processed}")
        print(f"  processing: {processing}")
        print(f"  pending:    {pending}")
        print(f"  failed:     {failed}")
        print(f"Not started: {total_papers - tracked}")
        print(f"Entities:  {entities}")
        print(f"Relations: {relations}")

        conn.close()
    except Exception as e:
        print(f"DB error: {e}")

    # 3. Log tail
    print()
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        print(f"Log: {len(lines)} lines, last 5:")
        for line in lines[-5:]:
            print(f"  {line.rstrip()}")
    else:
        print("Log: No log file found")


if __name__ == "__main__":
    check()
