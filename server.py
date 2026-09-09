#!/usr/bin/env python3
"""
Antigravity SQLite Database Inspector
A self-contained local web dashboard for inspecting Antigravity SQLite databases,
analyzing step payloads, detecting oversized bloat, and running SQL queries.
Zero external pip dependencies required.
"""

import http.server
import json
import os
import shutil
import pathlib
import re
import socketserver
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime

PORT = 8990
HOST = "0.0.0.0"

DESKTOP_CONVERSATIONS_DIR = pathlib.Path.home() / ".gemini" / "antigravity" / "conversations"
CLI_CONVERSATIONS_DIR = pathlib.Path.home() / ".gemini" / "antigravity-cli" / "conversations"

EXTRA_DB_DIRS = []

def get_all_search_dirs():
    dirs = [
        ("desktop", DESKTOP_CONVERSATIONS_DIR),
        ("cli", CLI_CONVERSATIONS_DIR),
    ]
    env_dirs = os.environ.get("ANTIGRAVITY_DB_DIRS") or os.environ.get("ANTIGRAVITY_DB_DIR")
    if env_dirs:
        for p in env_dirs.split(":"):
            if p.strip():
                pth = pathlib.Path(p.strip()).expanduser()
                dirs.append((pth.name or "custom", pth))
    for extra in EXTRA_DB_DIRS:
        dirs.append((extra.name or "custom", extra))
    return dirs


STEP_TYPE_MAP = {
    0: "UNSPECIFIED",
    3: "PLAN_INPUT",
    8: "VIEW_FILE",
    9: "LIST_DIRECTORY",
    14: "USER_INPUT",
    15: "PLANNER_RESPONSE",
    23: "CHECKPOINT",
    24: "PROPOSE_CODE",
    29: "MEMORY",
    34: "RETRIEVE_MEMORY",
    39: "MANAGER_FEEDBACK",
    40: "TOOL_CALL_PROPOSAL",
    41: "TOOL_CALL_CHOICE",
    42: "TRAJECTORY_CHOICE",
    49: "POST_PR_REVIEW",
    54: "FIND_ALL_REFERENCES",
    55: "BRAIN_UPDATE",
    59: "PROPOSAL_FEEDBACK",
    80: "BROWSER_PRESS_KEY",
    81: "TASK_BOUNDARY",
    82: "NOTIFY_USER",
    83: "CODE_ACKNOWLEDGEMENT",
    84: "INTERNAL_SEARCH",
    85: "BROWSER_SUBAGENT",
    86: "FILE_CHANGE",
    87: "MOVE",
    88: "BROWSER_SCROLL",
    89: "KNOWLEDGE_GENERATION",
    90: "EPHEMERAL_MESSAGE",
    91: "GENERATE_IMAGE",
    92: "DELETE_DIRECTORY",
    93: "COMPILE_APPLET",
    94: "INSTALL_APPLET_DEPENDENCIES",
    95: "INSTALL_APPLET_PACKAGE",
    96: "BROWSER_RESIZE_WINDOW",
    97: "BROWSER_DRAG_PIXEL_TO_PIXEL",
    98: "CONVERSATION_HISTORY",
    99: "KNOWLEDGE_ARTIFACTS",
    100: "SEND_COMMAND_INPUT",
    101: "SYSTEM_MESSAGE",
    102: "WAIT",
    103: "AGENCY_TOOL_CALL",
    104: "CIDER_AGENT_DUMMY",
    105: "BUILD_CLEANER",
    106: "BLAZE_BUILD_TARGETS",
    107: "BLAZE_TEST_TARGETS",
    108: "SET_UP_FIREBASE",
    109: "MOMA",
    110: "RESTART_DEV_SERVER",
    111: "DEPLOY_FIREBASE",
    112: "SHELL_EXEC",
    113: "BROWSER_MOUSE_WHEEL",
    114: "LINT_APPLET",
    116: "KI_INSERTION",
    117: "RETRIEVE_CONTENT",
    118: "CRITIQUE",
    119: "FINDINGS",
    120: "BROWSER_MOUSE_UP",
    121: "BROWSER_MOUSE_DOWN",
    122: "WORKSPACE_API",
    124: "BROWSER_GET_NETWORK_REQUEST",
    125: "BROWSER_REFRESH_PAGE",
    126: "EDIT_NOTEBOOK",
    127: "INVOKE_SUBAGENT",
    128: "WRITE_BLOB",
    129: "READ_NOTEBOOK",
    130: "PROPOSE_AI_COMMENTS",
    131: "START_CODE_REVIEW",
    132: "GENERIC",
    133: "SET_UP_CLOUD_SQL",
    134: "EXECUTE_NOTEBOOK",
    135: "CLOUD_SQL_UPDATE_SCHEMA",
    136: "RPC_ACTION",
    137: "CLOUD_SQL_EXECUTE_SQL",
    138: "ASK_QUESTION",
    139: "DIRECTORY_RULES",
    140: "TOOL_SEARCH",
}

STATUS_MAP = {
    0: "UNSPECIFIED",
    1: "PENDING",
    2: "RUNNING",
    3: "DONE",
    4: "INVALID",
    5: "CLEARED",
    6: "CANCELED",
    7: "ERROR",
    8: "GENERATING",
    9: "WAITING",
    10: "HALTED",
    11: "QUEUED",
    12: "INTERRUPTED",
}
def format_bytes(size):
    if size is None:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(size) < 1024.0:
            return f"{size:3.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def encode_varint(val):
    res = bytearray()
    while val > 0x7F:
        res.append((val & 0x7F) | 0x80)
        val >>= 7
    res.append(val & 0x7F)
    return bytes(res)

def encode_tag(fnum, wtype):
    return encode_varint((fnum << 3) | wtype)

def prune_payload(payload, threshold=500_000):
    """Recursively traverses protobuf wire format and prunes string/byte leaves > threshold."""
    if not payload or len(payload) <= threshold:
        return payload, 0

    fields = []
    pos = 0
    pruned_count = 0

    while pos < len(payload):
        k, pos = read_varint(payload, pos)
        fn, wt = k >> 3, k & 7
        if wt == 0:
            v, pos = read_varint(payload, pos)
            fields.append((fn, wt, v))
        elif wt in (1, 5):
            sz = 8 if wt == 1 else 4
            v = payload[pos:pos+sz]
            pos += sz
            fields.append((fn, wt, v))
        elif wt == 2:
            l, pos = read_varint(payload, pos)
            val = payload[pos:pos+l]
            pos += l

            if l > threshold:
                try:
                    sub_val, sub_pruned = prune_payload(val, threshold)
                    if sub_pruned > 0:
                        fields.append((fn, wt, sub_val))
                        pruned_count += sub_pruned
                        continue
                except Exception:
                    pass

                placeholder = f"\n[Pruned by Antigravity DB Inspector: original size was {l // 1024} KB. File diff/output preserved on disk.]\n".encode("utf-8")
                fields.append((fn, wt, placeholder))
                pruned_count += 1
            else:
                fields.append((fn, wt, val))
        else:
            break

    res = bytearray()
    for fn, wt, val in fields:
        res.extend(encode_tag(fn, wt))
        if wt == 0:
            res.extend(encode_varint(val))
        elif wt in (1, 5):
            res.extend(val)
        elif wt == 2:
            res.extend(encode_varint(len(val)))
            res.extend(val)

    return bytes(res), pruned_count

def read_varint(data, pos):
    val, shift = 0, 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return val, pos

def parse_protobuf_fields(data, max_depth=3, current_depth=0):
    """Parses raw protobuf wire format into a structural breakdown."""
    if not data or current_depth > max_depth:
        return []
    fields = []
    pos = 0
    total_len = len(data)

    while pos < total_len:
        start_pos = pos
        try:
            key, pos = read_varint(data, pos)
            field_num = key >> 3
            wire_type = key & 0x7

            if field_num == 0:
                break

            field_info = {
                "field": field_num,
                "wire_type": wire_type,
                "start": start_pos,
            }

            if wire_type == 0:  # Varint
                val, pos = read_varint(data, pos)
                field_info["val"] = val
                field_info["size"] = pos - start_pos
            elif wire_type == 1:  # 64-bit
                if pos + 8 > total_len:
                    break
                val = data[pos:pos+8]
                pos += 8
                field_info["size"] = 9
                field_info["hex"] = val.hex()
            elif wire_type == 2:  # Length-delimited
                length, pos = read_varint(data, pos)
                if pos + length > total_len:
                    break
                val_bytes = data[pos:pos+length]
                pos += length
                field_info["size"] = length
                field_info["size_formatted"] = format_bytes(length)

                # Try interpreting as UTF-8 string if small and clean
                is_text = False
                if length < 5000:
                    try:
                        text = val_bytes.decode('utf-8')
                        if all(c.isprintable() or c in '\r\n\t ' for c in text):
                            field_info["preview"] = text[:300] + ("..." if len(text) > 300 else "")
                            is_text = True
                    except UnicodeDecodeError:
                        pass

                # If not simple text and depth allows, try parsing as sub-message
                if not is_text and current_depth < max_depth and length > 4:
                    try:
                        sub = parse_protobuf_fields(val_bytes, max_depth, current_depth + 1)
                        if sub and sum(s["size"] for s in sub) >= length * 0.7:
                            field_info["subfields"] = sub
                    except Exception:
                        pass
            elif wire_type == 5:  # 32-bit
                if pos + 4 > total_len:
                    break
                val = data[pos:pos+4]
                pos += 4
                field_info["size"] = 5
                field_info["hex"] = val.hex()
            else:
                break

            fields.append(field_info)
        except Exception:
            break

    return fields

def get_db_path(name, source="desktop"):
    clean_name = os.path.basename(name)
    if not clean_name.endswith(".db"):
        clean_name += ".db"
    
    search_dirs = get_all_search_dirs()
    # Check matching source first
    for s_name, s_dir in search_dirs:
        if s_name == source and (s_dir / clean_name).is_file():
            return s_dir / clean_name
    # Fallback to search any directory
    for _, s_dir in search_dirs:
        if (s_dir / clean_name).is_file():
            return s_dir / clean_name

    raise FileNotFoundError(f"Database not found: {clean_name}")

class DatabaseService:
    @staticmethod
    def list_databases():
        results = []
        sources = get_all_search_dirs()

        for source_name, folder in sources:
            if not folder.exists():
                continue
            for file_path in folder.glob("*.db"):
                try:
                    stat = file_path.stat()
                    conv_id = file_path.stem
                    db_info = {
                        "name": file_path.name,
                        "conversation_id": conv_id,
                        "source": source_name,
                        "path": str(file_path),
                        "size_bytes": stat.st_size,
                        "size_formatted": format_bytes(stat.st_size),
                        "mtime": stat.st_mtime,
                        "mtime_formatted": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        "step_count": 0,
                        "max_step_bytes": 0,
                        "max_step_formatted": "0 B",
                    }

                    conn = sqlite3.connect(f"file:{file_path}?mode=ro", uri=True, timeout=1.0)
                    cursor = conn.cursor()
                    try:
                        cursor.execute("SELECT count(*), max(length(step_payload)) FROM steps")
                        row = cursor.fetchone()
                        if row:
                            db_info["step_count"] = row[0] or 0
                            max_bytes = row[1] or 0
                            db_info["max_step_bytes"] = max_bytes
                            db_info["max_step_formatted"] = format_bytes(max_bytes)
                    except sqlite3.Error:
                        pass
                    finally:
                        conn.close()

                    results.append(db_info)
                except Exception:
                    pass

        results.sort(key=lambda x: x["size_bytes"], reverse=True)
        return results

    @staticmethod
    def scan_bloat(threshold_bytes=1000000):
        bloat_items = []
        sources = get_all_search_dirs()

        for source_name, folder in sources:
            if not folder.exists():
                continue
            for file_path in folder.glob("*.db"):
                try:
                    conn = sqlite3.connect(f"file:{file_path}?mode=ro", uri=True, timeout=1.0)
                    c = conn.cursor()
                    c.execute("""
                        SELECT idx, step_type, status, length(step_payload) as sz
                        FROM steps
                        WHERE length(step_payload) >= ?
                        ORDER BY sz DESC
                    """, (threshold_bytes,))
                    rows = c.fetchall()
                    for idx, stype, status, sz in rows:
                        bloat_items.append({
                            "db_name": file_path.name,
                            "conversation_id": file_path.stem,
                            "source": source_name,
                            "step_idx": idx,
                            "step_type": STEP_TYPE_MAP.get(stype, f"TYPE_{stype}"),
                            "status": STATUS_MAP.get(status, f"STATUS_{status}"),
                            "size_bytes": sz,
                            "size_formatted": format_bytes(sz),
                        })
                    conn.close()
                except Exception:
                    pass

        bloat_items.sort(key=lambda x: x["size_bytes"], reverse=True)
        return bloat_items

    @staticmethod
    def get_database_summary(name, source="desktop"):
        path = get_db_path(name, source)
        stat = path.stat()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        c = conn.cursor()

        tables = []
        c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        for (tbl_name,) in c.fetchall():
            c.execute(f"SELECT count(*) FROM `{tbl_name}`")
            count = c.fetchone()[0]
            tables.append({"name": tbl_name, "rows": count})

        steps_stats = {"count": 0, "max_size": 0, "avg_size": 0}
        c.execute("""
            SELECT count(*), max(length(step_payload)), avg(length(step_payload))
            FROM steps
        """)
        s_row = c.fetchone()
        if s_row and s_row[0]:
            steps_stats = {
                "count": s_row[0],
                "max_size_bytes": s_row[1] or 0,
                "max_size_formatted": format_bytes(s_row[1] or 0),
                "avg_size_bytes": int(s_row[2] or 0),
                "avg_size_formatted": format_bytes(int(s_row[2] or 0)),
            }

        c.execute("PRAGMA integrity_check(1)")
        integrity = c.fetchone()[0]
        conn.close()

        return {
            "name": path.name,
            "conversation_id": path.stem,
            "source": source,
            "size_bytes": stat.st_size,
            "size_formatted": format_bytes(stat.st_size),
            "mtime_formatted": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "tables": tables,
            "steps_stats": steps_stats,
            "integrity": integrity,
        }

    @staticmethod
    def get_steps(name, source="desktop", sort_by="idx", order="asc", limit=100, offset=0):
        path = get_db_path(name, source)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        c = conn.cursor()

        sort_col = "length(step_payload)" if sort_by == "size" else "idx"
        sort_dir = "DESC" if order.lower() == "desc" else "ASC"

        c.execute("SELECT count(*) FROM steps")
        total_steps = c.fetchone()[0]

        query = f"""
            SELECT idx, step_type, status, has_subtrajectory,
                   length(step_payload) as sz,
                   length(metadata) as meta_sz
            FROM steps
            ORDER BY {sort_col} {sort_dir}
            LIMIT ? OFFSET ?
        """
        c.execute(query, (limit, offset))
        rows = c.fetchall()
        conn.close()

        steps = []
        for idx, stype, status, has_sub, sz, meta_sz in rows:
            steps.append({
                "idx": idx,
                "step_type": STEP_TYPE_MAP.get(stype, f"TYPE_{stype}"),
                "status": STATUS_MAP.get(status, f"STATUS_{status}"),
                "has_subtrajectory": bool(has_sub),
                "payload_size_bytes": sz or 0,
                "payload_size_formatted": format_bytes(sz or 0),
                "metadata_size_bytes": meta_sz or 0,
            })

        return {
            "total": total_steps,
            "limit": limit,
            "offset": offset,
            "steps": steps,
        }

    @staticmethod
    def get_step_detail(name, idx, source="desktop"):
        path = get_db_path(name, source)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        c = conn.cursor()
        c.execute("""
            SELECT idx, step_type, status, has_subtrajectory, step_format,
                   length(metadata), length(error_details), length(permissions),
                   length(task_details), length(render_info), length(step_payload),
                   step_payload
            FROM steps WHERE idx = ?
        """, (idx,))
        row = c.fetchone()
        conn.close()

        if not row:
            raise ValueError(f"Step {idx} not found")

        (s_idx, stype, status, has_sub, sfmt, meta_l, err_l, perm_l,
         task_l, rend_l, payload_l, payload_bytes) = row

        decoded_fields = []
        if payload_bytes:
            decoded_fields = parse_protobuf_fields(payload_bytes, max_depth=3)

        return {
            "idx": s_idx,
            "step_type": STEP_TYPE_MAP.get(stype, f"TYPE_{stype}"),
            "status": STATUS_MAP.get(status, f"STATUS_{status}"),
            "has_subtrajectory": bool(has_sub),
            "step_format": sfmt,
            "field_sizes": {
                "metadata": meta_l or 0,
                "error_details": err_l or 0,
                "permissions": perm_l or 0,
                "task_details": task_l or 0,
                "render_info": rend_l or 0,
                "step_payload": payload_l or 0,
                "step_payload_formatted": format_bytes(payload_l or 0),
            },
            "protobuf_breakdown": decoded_fields,
        }

    @staticmethod
    def execute_query(name, sql, source="desktop", confirm_write=False):
        path = get_db_path(name, source)
        cleaned_sql = sql.strip().rstrip(";")

        is_write = bool(re.search(r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|VACUUM)\b', cleaned_sql, re.IGNORECASE))
        if is_write and not confirm_write:
            raise PermissionError("Modifying queries require confirmation flag: confirm_write=True")

        uri = f"file:{path}" if is_write else f"file:{path}?mode=ro"
        start_t = time.time()
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        c = conn.cursor()
        c.execute(cleaned_sql)

        columns = []
        rows = []
        if c.description:
            columns = [d[0] for d in c.description]
            raw_rows = c.fetchmany(500)
            for r in raw_rows:
                row_fmt = []
                for val in r:
                    if isinstance(val, (bytes, bytearray)):
                        row_fmt.append(f"<BLOB {len(val)} bytes>")
                    else:
                        row_fmt.append(val)
                rows.append(row_fmt)

        if is_write:
            conn.commit()
        conn.close()
        elapsed_ms = round((time.time() - start_t) * 1000, 2)

        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "elapsed_ms": elapsed_ms,
        }


    @staticmethod
    def safe_prune_database(name, source="desktop", threshold=500_000):
        path = get_db_path(name, source)
        before_sz = path.stat().st_size

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = path.parent / f"{path.name}.bak_{ts}"
        shutil.copy2(str(path), str(backup_path))

        conn = sqlite3.connect(str(path), timeout=10.0)
        c = conn.cursor()

        c.execute("SELECT idx, step_payload FROM steps WHERE length(step_payload) >= ?", (threshold,))
        oversized_steps = c.fetchall()

        total_steps_pruned = 0
        total_items_pruned = 0

        for idx, payload in oversized_steps:
            if not payload:
                continue
            pruned_p, items_cnt = prune_payload(payload, threshold=threshold)
            if items_cnt > 0:
                c.execute("UPDATE steps SET step_payload = ? WHERE idx = ?", (pruned_p, idx))
                total_steps_pruned += 1
                total_items_pruned += items_cnt

        conn.commit()
        conn.execute("VACUUM")
        c.execute("PRAGMA integrity_check(1)")
        integrity = c.fetchone()[0]
        conn.close()

        after_sz = path.stat().st_size
        saved_sz = max(0, before_sz - after_sz)

        return {
            "name": path.name,
            "backup_path": str(backup_path),
            "steps_pruned": total_steps_pruned,
            "items_pruned": total_items_pruned,
            "before_bytes": before_sz,
            "before_formatted": format_bytes(before_sz),
            "after_bytes": after_sz,
            "after_formatted": format_bytes(after_sz),
            "saved_bytes": saved_sz,
            "saved_formatted": format_bytes(saved_sz),
            "integrity": integrity,
        }

    @staticmethod
    def safe_prune_step(name, idx, source="desktop", threshold=200_000):
        path = get_db_path(name, source)
        before_sz = path.stat().st_size

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = path.parent / f"{path.name}.bak_{ts}"
        shutil.copy2(str(path), str(backup_path))

        conn = sqlite3.connect(str(path), timeout=10.0)
        c = conn.cursor()
        c.execute("SELECT step_payload FROM steps WHERE idx = ?", (idx,))
        row = c.fetchone()
        if not row or not row[0]:
            conn.close()
            raise ValueError(f"Step {idx} has no payload")

        pruned_p, items_cnt = prune_payload(row[0], threshold=threshold)
        c.execute("UPDATE steps SET step_payload = ? WHERE idx = ?", (pruned_p, idx))
        conn.commit()
        conn.execute("VACUUM")
        c.execute("PRAGMA integrity_check(1)")
        integrity = c.fetchone()[0]
        conn.close()

        after_sz = path.stat().st_size
        return {
            "name": path.name,
            "step_idx": idx,
            "backup_path": str(backup_path),
            "items_pruned": items_cnt,
            "before_bytes": before_sz,
            "before_formatted": format_bytes(before_sz),
            "after_bytes": after_sz,
            "after_formatted": format_bytes(after_sz),
            "saved_bytes": max(0, before_sz - after_sz),
            "saved_formatted": format_bytes(max(0, before_sz - after_sz)),
            "integrity": integrity,
        }

    @staticmethod
    def vacuum_db(name, source="desktop"):
        path = get_db_path(name, source)
        before_sz = path.stat().st_size
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.execute("VACUUM")
        conn.close()
        after_sz = path.stat().st_size
        return {
            "name": path.name,
            "before_bytes": before_sz,
            "before_formatted": format_bytes(before_sz),
            "after_bytes": after_sz,
            "after_formatted": format_bytes(after_sz),
            "saved_bytes": before_sz - after_sz,
            "saved_formatted": format_bytes(before_sz - after_sz),
        }


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Antigravity DB Inspector</title>
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --accent-hover: #0284c7;
      --danger: #ef4444;
      --warning: #f59e0b;
      --success: #10b981;
      --code-bg: #090d16;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding-bottom: 60px;
    }
    header {
      background: #111827;
      border-bottom: 1px solid var(--card-border);
      padding: 1rem 2rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .brand { display: flex; align-items: center; gap: 0.75rem; font-weight: 700; font-size: 1.25rem; color: var(--accent); }
    .nav-tabs { display: flex; gap: 0.5rem; }
    .nav-btn {
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-muted);
      padding: 0.5rem 1rem;
      border-radius: 6px;
      cursor: pointer;
      font-weight: 500;
      transition: all 0.15s;
    }
    .nav-btn:hover { color: var(--text); background: var(--card-bg); }
    .nav-btn.active { color: #fff; background: #2563eb; border-color: #3b82f6; }

    main { max-width: 1400px; margin: 2rem auto; padding: 0 1.5rem; }

    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 1.5rem;
      margin-bottom: 1.5rem;
      box-shadow: 0 4px 6px -1px rgba(0,0,0,0.2);
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1rem;
      border-bottom: 1px solid var(--card-border);
      padding-bottom: 0.75rem;
    }
    .card-title { font-size: 1.15rem; font-weight: 600; color: #e2e8f0; }

    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 1rem;
      margin-bottom: 1.5rem;
    }
    .stat-card {
      background: #182234;
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 1rem;
    }
    .stat-label { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); }
    .stat-val { font-size: 1.5rem; font-weight: 700; margin-top: 0.25rem; color: #fff; }

    table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.9rem; }
    th { background: #0f172a; padding: 0.75rem 1rem; color: var(--text-muted); font-weight: 600; border-bottom: 1px solid var(--card-border); }
    td { padding: 0.75rem 1rem; border-bottom: 1px solid #283548; }
    tr:hover td { background: #233147; }

    .badge {
      display: inline-block;
      padding: 0.2rem 0.5rem;
      border-radius: 4px;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
    }
    .badge-danger { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid #ef4444; }
    .badge-warning { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid #f59e0b; }
    .badge-success { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; }
    .badge-neutral { background: rgba(148, 163, 184, 0.2); color: #cbd5e1; border: 1px solid #64748b; }

    .btn {
      background: var(--accent);
      color: #0f172a;
      border: none;
      padding: 0.45rem 0.9rem;
      border-radius: 6px;
      font-weight: 600;
      cursor: pointer;
      font-size: 0.85rem;
      transition: background 0.15s;
    }
    .btn:hover { background: var(--accent-hover); color: #fff; }
    .btn-secondary { background: #334155; color: #f8fafc; }
    .btn-secondary:hover { background: #475569; }
    .btn-danger { background: var(--danger); color: #fff; }
    .btn-danger:hover { background: #dc2626; }

    input, select, textarea {
      background: var(--code-bg);
      border: 1px solid var(--card-border);
      color: #fff;
      padding: 0.5rem 0.75rem;
      border-radius: 6px;
      font-size: 0.9rem;
      outline: none;
    }
    input:focus, select:focus, textarea:focus { border-color: var(--accent); }
    textarea { font-family: monospace; width: 100%; min-height: 100px; resize: vertical; }

    .size-bar-wrap { background: #334155; border-radius: 4px; height: 8px; width: 100px; overflow: hidden; display: inline-block; vertical-align: middle; margin-left: 8px; }
    .size-bar { height: 100%; border-radius: 4px; }
    .size-bar.safe { background: var(--success); }
    .size-bar.medium { background: var(--warning); }
    .size-bar.huge { background: var(--danger); }

    .tree { font-family: monospace; font-size: 0.85rem; background: var(--code-bg); padding: 1rem; border-radius: 6px; max-height: 450px; overflow-y: auto; }
    .tree-node { margin: 0.25rem 0; }
    .modal {
      display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
      background: rgba(0,0,0,0.7); z-index: 1000; align-items: center; justify-content: center;
    }
    .modal.active { display: flex; }
    .modal-content {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      width: 90%;
      max-width: 900px;
      max-height: 85vh;
      display: flex;
      flex-direction: column;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5);
    }
    .modal-header { padding: 1rem 1.5rem; border-bottom: 1px solid var(--card-border); display: flex; justify-content: space-between; align-items: center; }
    .modal-body { padding: 1.5rem; overflow-y: auto; flex: 1; }
    .close-btn { background: transparent; border: none; color: var(--text-muted); font-size: 1.5rem; cursor: pointer; }
    .close-btn:hover { color: #fff; }

    .actions-row { display: flex; gap: 0.75rem; align-items: center; margin-bottom: 1rem; }
    .search-input { flex: 1; max-width: 400px; }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v20M2 12h20M4 4l16 16M4 20L20 4"/></svg>
      <span>Antigravity DB Inspector</span>
    </div>
    <div class="nav-tabs">
      <button class="nav-btn active" onclick="showTab('databases')">Databases</button>
      <button class="nav-btn" onclick="showTab('bloat')">Bloat Scanner 🚨</button>
      <button class="nav-btn" onclick="showTab('detail')" id="tabDetailBtn" style="display:none;">DB Detail</button>
      <button class="nav-btn" onclick="showTab('sql')">SQL Console</button>
    </div>
  </header>

  <main>
    <!-- TAB: DATABASES -->
    <section id="tab-databases">
      <div class="stats-grid" id="dbGlobalStats">
        <div class="stat-card">
          <div class="stat-label">Total Databases</div>
          <div class="stat-val" id="statTotalDbs">...</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Total Disk Space</div>
          <div class="stat-val" id="statTotalDisk">...</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Largest Database</div>
          <div class="stat-val" id="statLargestDb" style="font-size: 1.2rem;">...</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <div class="card-title">All Antigravity Conversations</div>
          <div class="actions-row" style="margin-bottom:0;">
            <input type="text" class="search-input" id="dbSearch" placeholder="Search by ID or name..." oninput="filterDatabases()">
            <button class="btn btn-secondary" onclick="loadDatabases()">Refresh</button>
          </div>
        </div>
        <table id="dbTable">
          <thead>
            <tr>
              <th>Conversation ID</th>
              <th>Source</th>
              <th>Steps</th>
              <th>Total Size</th>
              <th>Max Step Size</th>
              <th>Last Modified</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="dbTableBody">
            <tr><td colspan="7" style="text-align:center;">Loading databases...</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- TAB: BLOAT SCANNER -->
    <section id="tab-bloat" style="display:none;">
      <div class="card">
        <div class="card-header">
          <div>
            <div class="card-title">Oversized Step Scanner</div>
            <p style="color:var(--text-muted); font-size:0.85rem; margin-top:4px;">
              Identifies steps whose payloads exceed safe WebChannel streaming limits.
            </p>
          </div>
          <div style="display:flex; gap:0.5rem; align-items:center;">
            <select id="bloatThreshold" class="search-input" style="width:auto; padding: 0.4rem 0.8rem; background: var(--bg-tertiary); border: 1px solid var(--card-border); color: var(--text); border-radius: 6px; font-size: 0.85rem;" onchange="runBloatScan()">
              <option value="250000">Threshold: > 250 KB</option>
              <option value="500000" selected>Threshold: > 500 KB</option>
              <option value="1000000">Threshold: > 1 MB</option>
            </select>
            <button class="btn btn-warning" onclick="pruneAllBloat()">⚡ Safe Prune All Bloat</button>
            <button class="btn btn-secondary" onclick="runBloatScan()">Refresh Scan</button>
          </div>
        </div>
        <table id="bloatTable">
          <thead>
            <tr>
              <th>Conversation ID</th>
              <th>Source</th>
              <th>Step Index</th>
              <th>Step Type</th>
              <th>Status</th>
              <th>Payload Size</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="bloatTableBody">
            <tr><td colspan="7" style="text-align:center;">Click "Scan All DBs Now" to scan.</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- TAB: DB DETAIL & STEPS -->
    <section id="tab-detail" style="display:none;">
      <div class="actions-row">
        <button class="btn btn-secondary" onclick="showTab('databases')">← Back to Databases</button>
        <span id="detailDbTitle" style="font-weight:600; font-size:1.1rem; color:var(--accent);"></span>
      </div>

      <div class="stats-grid" id="dbDetailStats">
        <div class="stat-card">
          <div class="stat-label">Total Steps</div>
          <div class="stat-val" id="dtTotalSteps">-</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">File Size</div>
          <div class="stat-val" id="dtFileSize">-</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Max Step Size</div>
          <div class="stat-val" id="dtMaxStep">-</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Integrity</div>
          <div class="stat-val" id="dtIntegrity">-</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <div class="card-title">Conversation Steps</div>
          <div class="actions-row" style="margin-bottom:0;">
            <label style="font-size:0.85rem; color:var(--text-muted);">Sort by:</label>
            <select id="stepSort" onchange="loadSteps()">
              <option value="idx_asc">Index (1 → N)</option>
              <option value="idx_desc">Index (N → 1)</option>
              <option value="size_desc" selected>Size (Largest First)</option>
            </select>
            <button class="btn btn-warning" onclick="pruneCurrentDbBloat()">⚡ Safe Prune Bloat</button>
            <button class="btn btn-secondary" onclick="vacuumCurrentDb()">Run VACUUM</button>
          </div>
        </div>
        <table id="stepsTable">
          <thead>
            <tr>
              <th>Index</th>
              <th>Step Type</th>
              <th>Status</th>
              <th>Payload Size</th>
              <th>Distribution</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="stepsTableBody"></tbody>
        </table>
      </div>
    </section>

    <!-- TAB: SQL CONSOLE -->
    <section id="tab-sql" style="display:none;">
      <div class="card">
        <div class="card-header">
          <div class="card-title">Interactive SQL Console</div>
          <div style="display:flex; gap:0.5rem;">
            <select id="sqlDbSelect" style="min-width: 250px;"></select>
          </div>
        </div>
        <div style="margin-bottom:1rem;">
          <textarea id="sqlInput" rows="4">SELECT idx, step_type, status, length(step_payload) as sz FROM steps ORDER BY sz DESC LIMIT 10</textarea>
        </div>
        <div class="actions-row">
          <button class="btn" onclick="executeSql()">Execute Query</button>
          <button class="btn btn-secondary" onclick="setSqlPreset('integrity')">Check Integrity</button>
          <button class="btn btn-secondary" onclick="setSqlPreset('largest_steps')">Top 20 Steps</button>
          <button class="btn btn-secondary" onclick="setSqlPreset('schema')">Show Schema</button>
          <span id="sqlStatus" style="font-size:0.85rem; color:var(--text-muted); margin-left:auto;"></span>
        </div>
        <div style="overflow-x:auto; margin-top:1rem;">
          <table id="sqlResultTable">
            <thead id="sqlResultHead"></thead>
            <tbody id="sqlResultBody"></tbody>
          </table>
        </div>
      </div>
    </section>
  </main>

  <!-- STEP INSPECTION MODAL -->
  <div class="modal" id="stepModal">
    <div class="modal-content">
      <div class="modal-header">
        <div style="font-weight:600; font-size:1.1rem;" id="modalStepTitle">Step Details</div>
        <button class="close-btn" onclick="closeModal()">&times;</button>
      </div>
      <div class="modal-body">
        <div class="stats-grid" id="modalFieldSizes" style="margin-bottom:1rem;"></div>
        <div style="margin-bottom:0.5rem; font-weight:600; color:var(--text-muted); font-size:0.85rem;">PROTOBUF WIRE STRUCTURE BREAKDOWN</div>
        <div class="tree" id="modalProtoTree"></div>
      </div>
    </div>
  </div>

  <script>
    let allDbs = [];
    let currentDb = null;
    let currentSource = "desktop";

    async function fetchApi(endpoint, options = {}) {
      try {
        const res = await fetch(endpoint, options);
        if (!res.ok) {
          const err = await res.json().catch(() => ({ error: res.statusText }));
          throw new Error(err.error || res.statusText);
        }
        return await res.json();
      } catch (err) {
        alert("API Error: " + err.message);
        throw err;
      }
    }

    function showTab(tabName) {
      document.querySelectorAll("main > section").forEach(s => s.style.display = "none");
      document.querySelectorAll(".nav-btn").forEach(b => b.classList.remove("active"));
      
      const tabEl = document.getElementById("tab-" + tabName);
      if (tabEl) tabEl.style.display = "block";
      
      const btn = Array.from(document.querySelectorAll(".nav-btn")).find(b => b.textContent.toLowerCase().includes(tabName));
      if (btn) btn.classList.add("active");

      if (tabName === 'bloat') runBloatScan();
      if (tabName === 'sql') populateSqlDbs();
    }

    async function loadDatabases() {
      const data = await fetchApi("/api/databases");
      allDbs = data;
      renderDatabases(allDbs);

      document.getElementById("statTotalDbs").textContent = allDbs.length;
      const totalBytes = allDbs.reduce((acc, d) => acc + d.size_bytes, 0);
      document.getElementById("statTotalDisk").textContent = formatBytes(totalBytes);
      if (allDbs.length > 0) {
        document.getElementById("statLargestDb").textContent = allDbs[0].size_formatted + " (" + allDbs[0].conversation_id.slice(0, 8) + ")";
      }
    }

    function renderDatabases(dbs) {
      const tbody = document.getElementById("dbTableBody");
      tbody.innerHTML = "";
      if (dbs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;">No databases found.</td></tr>';
        return;
      }

      dbs.forEach(db => {
        const tr = document.createElement("tr");
        const isOversized = db.max_step_bytes > 1000000;
        const maxStepBadge = isOversized
          ? `<span class="badge badge-danger">${db.max_step_formatted} 🚨</span>`
          : `<span class="badge badge-neutral">${db.max_step_formatted}</span>`;

        tr.innerHTML = `
          <td style="font-family:monospace; font-weight:600;">
            <a href="javascript:void(0)" onclick="openDatabase('${db.name}', '${db.source}')" style="color:var(--accent); text-decoration:none;">
              ${db.conversation_id}
            </a>
          </td>
          <td><span class="badge ${db.source === 'desktop' ? 'badge-neutral' : 'badge-warning'}">${db.source}</span></td>
          <td>${db.step_count.toLocaleString()}</td>
          <td style="font-weight:600;">${db.size_formatted}</td>
          <td>${maxStepBadge}</td>
          <td style="color:var(--text-muted); font-size:0.85rem;">${db.mtime_formatted}</td>
          <td>
            <button class="btn" onclick="openDatabase('${db.name}', '${db.source}')">Inspect</button>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    function filterDatabases() {
      const q = document.getElementById("dbSearch").value.toLowerCase();
      const filtered = allDbs.filter(d => d.name.toLowerCase().includes(q) || d.conversation_id.toLowerCase().includes(q));
      renderDatabases(filtered);
    }

    async function runBloatScan() {
      const tbody = document.getElementById("bloatTableBody");
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;">Scanning all conversation databases...</td></tr>';
      const threshEl = document.getElementById("bloatThreshold");
      const thresh = threshEl ? threshEl.value : 500000;
      const items = await fetchApi(`/api/scan_bloat?threshold=${thresh}`);
      tbody.innerHTML = "";
      if (items.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color:var(--success); font-weight:600;">🎉 No oversized steps (> ${formatBytes(thresh)}) detected in any database!</td></tr>`;
        return;
      }
      items.forEach(it => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td style="font-family:monospace; font-weight:600; color:var(--accent);">${it.conversation_id}</td>
          <td><span class="badge badge-neutral">${it.source}</span></td>
          <td style="font-weight:700;">${it.step_idx}</td>
          <td><span class="badge badge-neutral">${it.step_type}</span></td>
          <td><span class="badge badge-neutral">${it.status}</span></td>
          <td><span class="badge badge-danger">${it.size_formatted}</span></td>
          <td>
            <button class="btn btn-secondary" onclick="openDatabase('${it.db_name}', '${it.source}')">Inspect</button>
            <button class="btn" onclick="inspectStep('${it.db_name}', '${it.source}', ${it.step_idx})">View</button>
            <button class="btn btn-warning" onclick="pruneSingleStep('${it.db_name}', '${it.source}', ${it.step_idx})">⚡ Prune</button>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function openDatabase(name, source) {
      currentDb = name;
      currentSource = source;
      document.getElementById("tabDetailBtn").style.display = "inline-block";
      showTab("detail");
      document.getElementById("detailDbTitle").textContent = name + " (" + source + ")";

      const summary = await fetchApi(`/api/database?name=${encodeURIComponent(name)}&source=${source}`);
      document.getElementById("dtTotalSteps").textContent = summary.steps_stats.count.toLocaleString();
      document.getElementById("dtFileSize").textContent = summary.size_formatted;
      document.getElementById("dtMaxStep").textContent = summary.steps_stats.max_size_formatted;
      
      const intBadge = summary.integrity === 'ok'
        ? '<span style="color:var(--success)">OK</span>'
        : `<span style="color:var(--danger)">${summary.integrity}</span>`;
      document.getElementById("dtIntegrity").innerHTML = intBadge;

      loadSteps();
    }

    async function loadSteps() {
      if (!currentDb) return;
      const sortVal = document.getElementById("stepSort").value;
      let sort = "idx", order = "asc";
      if (sortVal === "idx_desc") { sort = "idx"; order = "desc"; }
      else if (sortVal === "size_desc") { sort = "size"; order = "desc"; }

      const tbody = document.getElementById("stepsTableBody");
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;">Loading steps...</td></tr>';

      const res = await fetchApi(`/api/steps?name=${encodeURIComponent(currentDb)}&source=${currentSource}&sort=${sort}&order=${order}&limit=100`);
      tbody.innerHTML = "";

      const maxSz = Math.max(...res.steps.map(s => s.payload_size_bytes), 1);

      res.steps.forEach(s => {
        const tr = document.createElement("tr");
        const pct = Math.min(100, Math.round((s.payload_size_bytes / maxSz) * 100));
        let barClass = "safe";
        if (s.payload_size_bytes > 1000000) barClass = "huge";
        else if (s.payload_size_bytes > 100000) barClass = "medium";

        let statusBadge = `<span class="badge badge-neutral">${s.status}</span>`;
        if (s.status === "DONE") statusBadge = `<span class="badge badge-success">DONE</span>`;
        else if (s.status === "ERROR") statusBadge = `<span class="badge badge-danger">ERROR</span>`;
        else if (s.status === "RUNNING") statusBadge = `<span class="badge badge-warning">RUNNING</span>`;
        else if (s.status === "CLEARED") statusBadge = `<span class="badge badge-neutral">CLEARED</span>`;

        tr.innerHTML = `
          <td style="font-weight:700;">#${s.idx}</td>
          <td><span class="badge badge-neutral">${s.step_type}</span></td>
          <td>${statusBadge}</td>
          <td style="font-weight:600;">${s.payload_size_formatted}</td>
          <td>
            <div class="size-bar-wrap">
              <div class="size-bar ${barClass}" style="width: ${Math.max(4, pct)}%;"></div>
            </div>
            <span style="font-size:0.75rem; color:var(--text-muted); margin-left:6px;">${pct}%</span>
          </td>
          <td>
            <button class="btn" onclick="inspectStep('${currentDb}', '${currentSource}', ${s.idx})">Inspect</button>
            ${s.payload_size_bytes > 500000 ? `<button class="btn btn-warning" onclick="pruneSingleStep('${currentDb}', '${currentSource}', ${s.idx})">⚡ Prune</button>` : ""}
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function inspectStep(dbName, source, idx) {
      const step = await fetchApi(`/api/step?name=${encodeURIComponent(dbName)}&source=${source}&idx=${idx}`);
      document.getElementById("modalStepTitle").textContent = `Step #${idx} Payload Breakdown (${step.step_type})`;

      const sizesEl = document.getElementById("modalFieldSizes");
      sizesEl.innerHTML = `
        <div class="stat-card">
          <div class="stat-label">Payload Size</div>
          <div class="stat-val" style="color:${step.field_sizes.step_payload > 1000000 ? 'var(--danger)' : 'var(--accent)'}">
            ${step.field_sizes.step_payload_formatted}
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Metadata Size</div>
          <div class="stat-val">${formatBytes(step.field_sizes.metadata)}</div>
        </div>
      `;

      const treeEl = document.getElementById("modalProtoTree");
      treeEl.innerHTML = renderProtoTree(step.protobuf_breakdown);
      document.getElementById("stepModal").classList.add("active");
    }

    function renderProtoTree(fields, indent = 0) {
      if (!fields || fields.length === 0) return '<div style="color:var(--text-muted);">No structured fields or empty payload.</div>';
      let html = '';
      fields.forEach(f => {
        const indentPx = indent * 20;
        const sizeBadge = f.size ? `<span style="color:var(--warning); margin-left:8px;">[${formatBytes(f.size)}]</span>` : '';
        const preview = f.preview ? `<span style="color:#a5f3fc; margin-left:8px;">"${escapeHtml(f.preview)}"</span>` : '';
        const val = f.val !== undefined ? `<span style="color:#fde047; margin-left:8px;">val: ${f.val}</span>` : '';
        
        html += `<div class="tree-node" style="padding-left:${indentPx}px;">`;
        html += `<strong>Field ${f.field}</strong> (wire ${f.wire_type})${sizeBadge}${val}${preview}`;
        if (f.subfields && f.subfields.length > 0) {
          html += renderProtoTree(f.subfields, indent + 1);
        }
        html += `</div>`;
      });
      return html;
    }

    function closeModal() {
      document.getElementById("stepModal").classList.remove("active");
    }

    async function vacuumCurrentDb() {
      if (!currentDb) return;
      if (!confirm(`Run VACUUM on ${currentDb}? This will rewrite the database file on disk.`)) return;
      const res = await fetchApi("/api/vacuum", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: currentDb, source: currentSource })
      });
      alert(`VACUUM finished! Reduced size from ${res.before_formatted} to ${res.after_formatted} (Saved ${res.saved_formatted}).`);
      openDatabase(currentDb, currentSource);
    }

    function populateSqlDbs() {
      const select = document.getElementById("sqlDbSelect");
      select.innerHTML = "";
      allDbs.forEach(d => {
        const opt = document.createElement("option");
        opt.value = JSON.stringify({ name: d.name, source: d.source });
        opt.textContent = `${d.conversation_id.slice(0, 8)}... (${d.size_formatted})`;
        if (currentDb && d.name === currentDb) opt.selected = true;
        select.appendChild(opt);
      });
    }

    function setSqlPreset(type) {
      const area = document.getElementById("sqlInput");
      if (type === 'integrity') area.value = "PRAGMA integrity_check;";
      else if (type === 'largest_steps') area.value = "SELECT idx, step_type, status, length(step_payload) as payload_bytes FROM steps ORDER BY payload_bytes DESC LIMIT 20;";
      else if (type === 'schema') area.value = "SELECT type, name, sql FROM sqlite_master WHERE type='table';";
    }

    async function executeSql() {
      const dbSelect = document.getElementById("sqlDbSelect").value;
      if (!dbSelect) { alert("Select a database first"); return; }
      const { name, source } = JSON.parse(dbSelect);
      const query = document.getElementById("sqlInput").value;
      const statusEl = document.getElementById("sqlStatus");
      statusEl.textContent = "Executing query...";

      try {
        const res = await fetchApi("/api/query", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, source, query })
        });
        statusEl.textContent = `${res.row_count} rows returned in ${res.elapsed_ms} ms`;

        const head = document.getElementById("sqlResultHead");
        const body = document.getElementById("sqlResultBody");
        head.innerHTML = "";
        body.innerHTML = "";

        if (res.columns.length > 0) {
          const htr = document.createElement("tr");
          res.columns.forEach(c => {
            const th = document.createElement("th");
            th.textContent = c;
            htr.appendChild(th);
          });
          head.appendChild(htr);

          res.rows.forEach(r => {
            const btr = document.createElement("tr");
            r.forEach(val => {
              const td = document.createElement("td");
              td.textContent = val !== null ? val : "NULL";
              btr.appendChild(td);
            });
            body.appendChild(btr);
          });
        } else {
          body.innerHTML = '<tr><td style="color:var(--text-muted);">Query executed successfully (no result set).</td></tr>';
        }
      } catch (err) {
        statusEl.textContent = "Error: " + err.message;
      }
    }

    function formatBytes(bytes) {
      if (!bytes) return "0 B";
      const units = ["B", "KB", "MB", "GB"];
      let sz = bytes;
      for (let u of units) {
        if (Math.abs(sz) < 1024.0) return sz.toFixed(1) + " " + u;
        sz /= 1024.0;
      }
      return sz.toFixed(1) + " TB";
    }

    function escapeHtml(str) {
      return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    
    async function pruneSingleStep(name, source, idx) {
      if (!confirm(`⚡ Safe Prune Step #${idx} in ${name}?\n\n- Creates an automatic safety backup (.bak)\n- Replaces oversized file diff/stdout dumps (>200KB) with clean placeholders\n- Preserves all prompts, thinking, and code changes intact\n- Reclaims disk space immediately`)) return;
      try {
        const res = await fetchApi("/api/prune_step", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, source, idx, threshold: 200000 })
        });
        alert(`✅ Step #${idx} pruned successfully!\n\nSpace saved: ${res.saved_formatted}\nNew size: ${res.after_formatted}\nBackup: ${res.backup_path}`);
        if (currentDb === name) loadSteps();
        runBloatScan();
      } catch (err) {
        alert("Pruning failed: " + err.message);
      }
    }

    async function pruneCurrentDbBloat() {
      if (!currentDb) return;
      if (!confirm(`⚡ Safe Prune all oversized steps in ${currentDb}?\n\n- Creates an automatic safety backup (.bak)\n- Prunes bloated diffs and file dumps (>500KB)\n- Keeps all messages, thoughts, and instructions intact\n- Runs VACUUM automatically`)) return;
      try {
        const res = await fetchApi("/api/prune_db", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: currentDb, source: currentSource, threshold: 500000 })
        });
        alert(`✅ Pruning completed!\n\nSteps pruned: ${res.steps_pruned}\nSpace saved: ${res.saved_formatted}\nNew size: ${res.after_formatted}\nBackup: ${res.backup_path}`);
        openDatabase(currentDb, currentSource);
      } catch (err) {
        alert("Pruning failed: " + err.message);
      }
    }

    async function pruneAllBloat() {
      const items = await fetchApi("/api/scan_bloat");
      if (items.length === 0) {
        alert("🎉 No bloated steps (>1 MB) detected in any database!");
        return;
      }
      const uniqueDbs = [...new Set(items.map(i => JSON.stringify({ name: i.db_name, source: i.source })))].map(s => JSON.parse(s));
      if (!confirm(`⚡ Safe Prune Bloat across ${uniqueDbs.length} database(s) with oversized steps?\n\n- Creates automatic safety backups for each DB\n- Prunes oversized diffs and snapshots (>500KB)\n- Fully preserves all context, goals, and history`)) return;
      
      let totalSaved = 0;
      for (const db of uniqueDbs) {
        try {
          const res = await fetchApi("/api/prune_db", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: db.name, source: db.source, threshold: 500000 })
          });
          totalSaved += res.saved_bytes;
        } catch (e) {
          console.error(e);
        }
      }
      alert(`🎉 All bloated databases safely pruned!\nTotal disk space saved: ${formatBytes(totalSaved)}`);
      runBloatScan();
      loadDatabases();
    }

    window.onload = () => {
      loadDatabases();
    };
  </script>
</body>
</html>
"""

class RequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message, status=400):
        self.send_json({"error": str(message)}, status=status)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/databases":
            try:
                dbs = DatabaseService.list_databases()
                self.send_json(dbs)
            except Exception as e:
                self.send_error_json(e, 500)
            return

        if path == "/api/scan_bloat":
            try:
                t_str = query.get("threshold", ["500000"])[0]
                threshold = int(t_str) if t_str.isdigit() else 500_000
                items = DatabaseService.scan_bloat(threshold_bytes=threshold)
                self.send_json(items)
            except Exception as e:
                self.send_error_json(e, 500)
            return

        if path == "/api/database":
            name = query.get("name", [""])[0]
            source = query.get("source", ["desktop"])[0]
            if not name:
                self.send_error_json("Parameter 'name' required")
                return
            try:
                summary = DatabaseService.get_database_summary(name, source)
                self.send_json(summary)
            except Exception as e:
                self.send_error_json(e, 404)
            return

        if path == "/api/steps":
            name = query.get("name", [""])[0]
            source = query.get("source", ["desktop"])[0]
            sort_by = query.get("sort", ["idx"])[0]
            order = query.get("order", ["asc"])[0]
            limit = int(query.get("limit", [100])[0])
            offset = int(query.get("offset", [0])[0])
            if not name:
                self.send_error_json("Parameter 'name' required")
                return
            try:
                steps = DatabaseService.get_steps(name, source, sort_by, order, limit, offset)
                self.send_json(steps)
            except Exception as e:
                self.send_error_json(e, 404)
            return

        if path == "/api/step":
            name = query.get("name", [""])[0]
            source = query.get("source", ["desktop"])[0]
            idx_str = query.get("idx", [""])[0]
            if not name or not idx_str:
                self.send_error_json("Parameters 'name' and 'idx' required")
                return
            try:
                detail = DatabaseService.get_step_detail(name, int(idx_str), source)
                self.send_json(detail)
            except Exception as e:
                self.send_error_json(e, 404)
            return

        self.send_error_json("Not Found", 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"

        try:
            params = json.loads(body.decode("utf-8"))
        except Exception:
            params = {}

        if path == "/api/query":
            name = params.get("name", "")
            source = params.get("source", "desktop")
            sql = params.get("query", "")
            confirm_write = params.get("confirm_write", False)
            if not name or not sql:
                self.send_error_json("Fields 'name' and 'query' required")
                return
            try:
                result = DatabaseService.execute_query(name, sql, source, confirm_write)
                self.send_json(result)
            except Exception as e:
                self.send_error_json(e, 400)
            return

        
        if path == "/api/prune_db":
            name = params.get("name", "")
            source = params.get("source", "desktop")
            threshold = int(params.get("threshold", 500000))
            if not name:
                self.send_error_json("Field 'name' required")
                return
            try:
                res = DatabaseService.safe_prune_database(name, source, threshold)
                self.send_json(res)
            except Exception as e:
                self.send_error_json(e, 500)
            return

        if path == "/api/prune_step":
            name = params.get("name", "")
            source = params.get("source", "desktop")
            idx = int(params.get("idx", 0))
            threshold = int(params.get("threshold", 200000))
            if not name:
                self.send_error_json("Field 'name' required")
                return
            try:
                res = DatabaseService.safe_prune_step(name, idx, source, threshold)
                self.send_json(res)
            except Exception as e:
                self.send_error_json(e, 500)
            return

        if path == "/api/vacuum":
            name = params.get("name", "")
            source = params.get("source", "desktop")
            if not name:
                self.send_error_json("Field 'name' required")
                return
            try:
                res = DatabaseService.vacuum_db(name, source)
                self.send_json(res)
            except Exception as e:
                self.send_error_json(e, 500)
            return

        self.send_error_json("Not Found", 404)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Antigravity SQLite DB Inspector")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port to bind (default: {PORT})")
    parser.add_argument("--host", type=str, default=HOST, help=f"Host to bind (default: {HOST})")
    parser.add_argument("--db-dir", type=str, action="append", default=[], help="Additional database directory to scan")
    args = parser.parse_args()

    for d in args.db_dir:
        pth = pathlib.Path(d).expanduser().resolve()
        if pth.is_dir():
            EXTRA_DB_DIRS.append(pth)
        else:
            print(f"Warning: specified --db-dir does not exist: {pth}")

    server = ThreadedHTTPServer((args.host, args.port), RequestHandler)
    print(f"==================================================")
    print(f" Antigravity SQLite Database Inspector")
    print(f" Web UI running at: http://localhost:{args.port}/")
    print(f" Listening on:      {args.host}:{args.port}")
    print(f"")
    print(f" Discovered Database Locations:")
    for s_name, s_dir in get_all_search_dirs():
        if s_dir.exists():
            cnt = len(list(s_dir.glob("*.db")))
            print(f"   [{s_name:7s}] {s_dir} ({cnt} DBs)")
        else:
            print(f"   [{s_name:7s}] {s_dir} (not found)")
    print(f"==================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down inspector server...")
        server.server_close()

if __name__ == "__main__":
    main()
