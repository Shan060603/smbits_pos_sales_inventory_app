import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone


_DB_PATH = os.path.join(os.path.dirname(__file__), "offline_queue.db")
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _conn():
    db = sqlite3.connect(_DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def init_outbox():
    with _conn() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                context_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                processed_at TEXT
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_outbox_jobs_status_created ON outbox_jobs(status, created_at)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox_snapshots (
                snapshot_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def enqueue_job(job_type, payload, context):
    now = _utc_now()
    with _conn() as db:
        cur = db.execute(
            """
            INSERT INTO outbox_jobs (job_type, payload_json, context_json, status, attempts, last_error, created_at, updated_at, processed_at)
            VALUES (?, ?, ?, 'pending', 0, NULL, ?, ?, NULL)
            """,
            (job_type, json.dumps(payload), json.dumps(context), now, now),
        )
        return int(cur.lastrowid)


def is_transient_error(message):
    msg = (message or "").lower()
    transient_markers = [
        "timed out",
        "timeout",
        "connection",
        "network is unreachable",
        "name or service not known",
        "temporary failure",
        "max retries exceeded",
        "connection refused",
        "502",
        "503",
        "504",
        "proxyerror",
    ]
    return any(marker in msg for marker in transient_markers)


def _extract_error(result):
    if result is None:
        return "Empty ERP response"
    if isinstance(result, dict):
        msg = result.get("error") or result.get("message")
        if isinstance(msg, dict):
            return msg.get("message") or str(msg)
        return str(msg or result)
    return str(result)


def _execute_sales_submit(payload, context):
    from my_sales_app.bridge import SMBITSBridge

    bridge = SMBITSBridge(
        url=context.get("erp_url"),
        sid=context.get("erp_sid"),
        csrf_token=context.get("erp_csrf_token"),
    )
    result = bridge.send_sales_invoice(
        customer=payload.get("customer"),
        company=payload.get("company"),
        items=payload.get("items") or [],
        discount=payload.get("discount", 0),
        posting_date=payload.get("posting_date"),
        due_date=payload.get("due_date"),
        mode_of_payment=payload.get("mode_of_payment"),
        paid_amount=payload.get("paid_amount", 0),
        project=payload.get("project"),
        cost_center=payload.get("cost_center"),
    )
    doc = result.get("message") if isinstance(result, dict) else None
    if not isinstance(doc, dict):
        doc = result.get("data") if isinstance(result, dict) else None
    if isinstance(doc, dict) and doc.get("name"):
        return True, None
    return False, _extract_error(result)


def _execute_purchase_submit(payload, context):
    from my_purchases_app.bridge import SMBITSPurchaseBridge

    bridge = SMBITSPurchaseBridge(
        url=context.get("erp_url"),
        sid=context.get("erp_sid"),
        csrf_token=context.get("erp_csrf_token"),
    )
    result = bridge.send_purchase_order(
        supplier=payload.get("supplier"),
        company=payload.get("company"),
        items=payload.get("items") or [],
        transaction_date=payload.get("transaction_date"),
        schedule_date=payload.get("schedule_date"),
        submit=True,
    )
    doc = result.get("message") if isinstance(result, dict) else None
    if not isinstance(doc, dict):
        doc = result.get("data") if isinstance(result, dict) else None
    if isinstance(doc, dict) and doc.get("name"):
        return True, None
    return False, _extract_error(result)


def _run_job(job_type, payload, context):
    if job_type == "sales_submit_invoice":
        return _execute_sales_submit(payload, context)
    if job_type == "purchase_submit_order":
        return _execute_purchase_submit(payload, context)
    if job_type == "sales_create_item":
        from my_sales_app.bridge import SMBITSBridge

        bridge = SMBITSBridge(
            url=context.get("erp_url"),
            sid=context.get("erp_sid"),
            csrf_token=context.get("erp_csrf_token"),
        )
        result = bridge.create_item(
            item_code=payload.get("item_code"),
            item_name=payload.get("item_name"),
            stock_uom=payload.get("stock_uom"),
            sales_price=float(payload.get("sales_price") or 0),
            purchase_price=float(payload.get("purchase_price") or 0),
        )
        if isinstance(result, dict) and result.get("ok"):
            return True, None
        return False, _extract_error(result)
    if job_type == "purchase_create_item":
        from my_purchases_app.bridge import SMBITSPurchaseBridge

        bridge = SMBITSPurchaseBridge(
            url=context.get("erp_url"),
            sid=context.get("erp_sid"),
            csrf_token=context.get("erp_csrf_token"),
        )
        result = bridge.create_item(
            item_code=payload.get("item_code"),
            item_name=payload.get("item_name"),
            stock_uom=payload.get("stock_uom"),
            sales_price=float(payload.get("sales_price") or 0),
            purchase_price=float(payload.get("purchase_price") or 0),
        )
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            return True, None
        return False, _extract_error(result)
    if job_type == "inventory_create_item":
        from my_inventory_app.bridge import SMBITSInventoryBridge

        bridge = SMBITSInventoryBridge(
            url=context.get("erp_url"),
            sid=context.get("erp_sid"),
            csrf_token=context.get("erp_csrf_token"),
        )
        result = bridge.create_item(
            item_code=payload.get("item_code"),
            item_name=payload.get("item_name"),
            stock_uom=payload.get("stock_uom"),
            item_group=payload.get("item_group"),
            is_stock_item=bool(payload.get("is_stock_item", True)),
            sales_price=float(payload.get("sales_price") or 0),
            purchase_price=float(payload.get("purchase_price") or 0),
            barcodes=payload.get("barcodes") or [],
        )
        if isinstance(result, dict) and result.get("ok"):
            return True, None
        return False, _extract_error(result)
    return False, f"Unknown job type: {job_type}"


def process_pending_jobs(limit=20):
    now = _utc_now()
    processed = 0
    with _conn() as db:
        rows = db.execute(
            """
            SELECT id, job_type, payload_json, context_json, attempts
            FROM outbox_jobs
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

        for row in rows:
            job_id = int(row["id"])
            payload = json.loads(row["payload_json"] or "{}")
            context = json.loads(row["context_json"] or "{}")
            ok, error_text = _run_job(row["job_type"], payload, context)
            if ok:
                db.execute(
                    """
                    UPDATE outbox_jobs
                    SET status='done', processed_at=?, updated_at=?, last_error=NULL
                    WHERE id=?
                    """,
                    (now, now, job_id),
                )
                processed += 1
                continue

            attempts = int(row["attempts"] or 0) + 1
            if is_transient_error(error_text):
                db.execute(
                    """
                    UPDATE outbox_jobs
                    SET attempts=?, last_error=?, updated_at=?
                    WHERE id=?
                    """,
                    (attempts, (error_text or "")[:1200], now, job_id),
                )
            else:
                db.execute(
                    """
                    UPDATE outbox_jobs
                    SET status='failed', attempts=?, last_error=?, updated_at=?
                    WHERE id=?
                    """,
                    (attempts, (error_text or "")[:1200], now, job_id),
                )
    return processed


def outbox_status():
    with _conn() as db:
        counts = dict(
            db.execute(
                """
                SELECT status, COUNT(*) AS c
                FROM outbox_jobs
                GROUP BY status
                """
            ).fetchall()
        )
    return {
        "pending": int(counts.get("pending", 0)),
        "done": int(counts.get("done", 0)),
        "failed": int(counts.get("failed", 0)),
    }


def save_snapshot(snapshot_key, payload):
    now = _utc_now()
    with _conn() as db:
        db.execute(
            """
            INSERT INTO outbox_snapshots(snapshot_key, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(snapshot_key)
            DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """,
            (snapshot_key, json.dumps(payload), now),
        )


def load_snapshot(snapshot_key):
    with _conn() as db:
        row = db.execute(
            "SELECT payload_json FROM outbox_snapshots WHERE snapshot_key = ?",
            (snapshot_key,),
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload_json"])
        except Exception:
            return None


def _worker_loop(interval_seconds=20):
    while True:
        try:
            process_pending_jobs(limit=25)
        except Exception:
            pass
        time.sleep(max(5, int(interval_seconds)))


def start_outbox_worker(interval_seconds=20):
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        thread = threading.Thread(
            target=_worker_loop,
            kwargs={"interval_seconds": interval_seconds},
            daemon=True,
            name="offline-outbox-worker",
        )
        thread.start()
        _WORKER_STARTED = True
