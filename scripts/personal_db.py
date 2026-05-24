#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = PLUGIN_ROOT.parents[1] if len(PLUGIN_ROOT.parents) > 1 else Path.cwd()
LOCAL_WORKSPACE_DB = WORKSPACE / "personal-data" / "db" / "main.sqlite"
FALLBACK_DB = Path.home() / ".local" / "share" / "openclaw-personal-database" / "main.sqlite"
SCHEMA_VERSION = 2
FTS_TABLES = {"entities_fts", "library_items_fts", "text_chunks_fts"}
CORE_TABLES = {
    "sources", "raw_imports", "entities", "events", "facts", "links", "tags", "text_chunks",
    "library_items", "library_highlights", "digests", "digest_items", "migrations", *FTS_TABLES,
}
WRITE_SQL_RE = re.compile(r"\b(attach|alter|create|delete|detach|drop|insert|pragma|replace|update|vacuum)\b", re.I)


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_id(prefix, value):
    return f"{prefix}_{hashlib.sha256(str(value).encode()).hexdigest()[:24]}"


def json_dumps(value):
    return json.dumps(value if value is not None else {}, separators=(",", ":"), ensure_ascii=False)


def json_loads(value, default=None):
    if value is None or value == "":
        return {} if default is None else default
    try:
        return json.loads(value)
    except Exception:
        return {} if default is None else default


def parse_json_object(value, label="metadata"):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit(f"{label} must be a JSON object")
    return parsed


def normalize_url(url):
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        query = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not re.match(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$)", k, re.I)
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except Exception:
        return text


def title_from_url(url):
    try:
        parts = urlsplit(url)
        tail = [part for part in parts.path.split("/") if part][-1:]
        suffix = (" - " + tail[0].replace("-", " ").replace("_", " ")) if tail else ""
        return parts.netloc.removeprefix("www.") + suffix
    except Exception:
        return str(url or "Untitled")[:140]


def canonical_key(kind, title=None, url=None, key=None):
    if key:
        return key
    normalized = normalize_url(url)
    if normalized:
        return "url:" + normalized
    text = re.sub(r"\s+", " ", str(title or "untitled").strip().lower())
    return f"{kind}:{text}"


def resolved_db_path(args):
    if args.db:
        return Path(args.db).expanduser().resolve()
    env_path = os.environ.get("PERSONAL_DATABASE_DB") or os.environ.get("PERSONAL_DATA_DB")
    if env_path:
        return Path(env_path).expanduser().resolve()
    if LOCAL_WORKSPACE_DB.exists() or (WORKSPACE / "personal-data").exists():
        return LOCAL_WORKSPACE_DB
    return FALLBACK_DB


def connect(args):
    db_path = resolved_db_path(args)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def schema_version(conn):
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def set_schema_version(conn, version):
    conn.execute(f"PRAGMA user_version = {int(version)}")


def record_migration(conn, version, name):
    conn.execute(
        "INSERT OR IGNORE INTO migrations (version, name, applied_at) VALUES (?, ?, ?)",
        (version, name, now_iso()),
    )


def fts_query(text):
    tokens = re.findall(r"[A-Za-z0-9_]+", text or "")
    return " ".join(f'"{token}"' for token in tokens)


def fts_available(conn):
    try:
        conn.execute("DROP TABLE IF EXISTS temp.personal_db_fts_probe")
        conn.execute("CREATE VIRTUAL TABLE temp.personal_db_fts_probe USING fts5(value)")
        conn.execute("DROP TABLE temp.personal_db_fts_probe")
        return True
    except sqlite3.Error:
        return False


def ensure_fts_schema(conn):
    if not fts_available(conn):
        return False
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(entity_id UNINDEXED, type UNINDEXED, title, subtitle, url, metadata, tokenize='unicode61');
        CREATE VIRTUAL TABLE IF NOT EXISTS library_items_fts USING fts5(library_item_id UNINDEXED, entity_id UNINDEXED, title, author, publisher, url, tags, tokenize='unicode61');
        CREATE VIRTUAL TABLE IF NOT EXISTS text_chunks_fts USING fts5(chunk_id UNINDEXED, entity_id UNINDEXED, title, chunk_text, tokenize='unicode61');
        """
    )
    return True


def rebuild_fts(conn):
    if not ensure_fts_schema(conn):
        return {"available": False, "rebuilt": False}
    conn.execute("DELETE FROM entities_fts")
    conn.execute("DELETE FROM library_items_fts")
    conn.execute("DELETE FROM text_chunks_fts")
    conn.execute(
        "INSERT INTO entities_fts (entity_id, type, title, subtitle, url, metadata) SELECT id, type, title, coalesce(subtitle, ''), coalesce(url, ''), metadata FROM entities"
    )
    conn.execute(
        "INSERT INTO library_items_fts (library_item_id, entity_id, title, author, publisher, url, tags) SELECT id, entity_id, title, coalesce(author, ''), coalesce(publisher, ''), coalesce(url, ''), tags FROM library_items"
    )
    conn.execute(
        "INSERT INTO text_chunks_fts (chunk_id, entity_id, title, chunk_text) SELECT text_chunks.id, text_chunks.entity_id, entities.title, text_chunks.chunk_text FROM text_chunks JOIN entities ON entities.id = text_chunks.entity_id"
    )
    return {"available": True, "rebuilt": True}


def init_schema(conn):
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SystemExit(f"Database schema version {current} is newer than this CLI supports ({SCHEMA_VERSION})")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE TABLE IF NOT EXISTS raw_imports (id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), original_path TEXT, sha256 TEXT, metadata TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS entities (id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_key TEXT NOT NULL, title TEXT NOT NULL, subtitle TEXT, url TEXT, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), UNIQUE (type, canonical_key));
        CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, entity_id TEXT REFERENCES entities(id) ON DELETE CASCADE, source_id TEXT REFERENCES sources(id), type TEXT NOT NULL, occurred_at TEXT, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE TABLE IF NOT EXISTS facts (id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE, key TEXT NOT NULL, value TEXT NOT NULL, source_id TEXT REFERENCES sources(id), confidence REAL NOT NULL DEFAULT 1.0, observed_at TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE TABLE IF NOT EXISTS links (id TEXT PRIMARY KEY, from_entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE, to_entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE, type TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), UNIQUE (from_entity_id, to_entity_id, type));
        CREATE TABLE IF NOT EXISTS tags (entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE, tag TEXT NOT NULL, source_id TEXT REFERENCES sources(id), created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), PRIMARY KEY (entity_id, tag));
        CREATE TABLE IF NOT EXISTS text_chunks (id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE, source_id TEXT REFERENCES sources(id), chunk_index INTEGER NOT NULL, chunk_text TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), UNIQUE (entity_id, source_id, chunk_index));
        CREATE TABLE IF NOT EXISTS library_items (id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE, source_id TEXT NOT NULL REFERENCES sources(id), source_item_id TEXT, title TEXT NOT NULL, author TEXT, publisher TEXT, url TEXT, normalized_url TEXT, tags TEXT NOT NULL DEFAULT '[]', word_count INTEGER, in_queue INTEGER NOT NULL DEFAULT 0, favorited INTEGER NOT NULL DEFAULT 0, read INTEGER NOT NULL DEFAULT 0, highlight_count INTEGER NOT NULL DEFAULT 0, last_interaction_at TEXT, content_file_id TEXT, content_path TEXT, status TEXT NOT NULL DEFAULT 'imported', metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE UNIQUE INDEX IF NOT EXISTS library_items_normalized_url_idx ON library_items(normalized_url) WHERE normalized_url IS NOT NULL AND normalized_url <> '';
        CREATE TABLE IF NOT EXISTS library_highlights (id TEXT PRIMARY KEY, library_item_id TEXT REFERENCES library_items(id) ON DELETE CASCADE, source_id TEXT NOT NULL REFERENCES sources(id), title TEXT NOT NULL, author TEXT, publisher TEXT, text TEXT NOT NULL, note TEXT, highlighted_at TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE TABLE IF NOT EXISTS digests (id TEXT PRIMARY KEY, digest_date TEXT NOT NULL UNIQUE, title TEXT, status TEXT NOT NULL DEFAULT 'draft', epub_path TEXT, sent_at TEXT, qa_passed INTEGER, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE TABLE IF NOT EXISTS digest_items (digest_id TEXT NOT NULL REFERENCES digests(id) ON DELETE CASCADE, library_item_id TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE, position INTEGER NOT NULL, chapter_path TEXT, build_mode TEXT, metadata TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (digest_id, position));
        """
    )
    if current < 1:
        record_migration(conn, 1, "core schema")
    if current < 2:
        rebuild_fts(conn)
        record_migration(conn, 2, "fts search indexes")
    else:
        ensure_fts_schema(conn)
    set_schema_version(conn, SCHEMA_VERSION)
    conn.commit()


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def is_fts_shadow_table(name):
    return any(name.startswith(f"{table}_") for table in FTS_TABLES)


def user_table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [row["name"] for row in rows if not is_fts_shadow_table(row["name"])]


def print_result(args, result):
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif isinstance(result, list):
        if not result:
            print("No results")
        for row in result:
            print(" ".join(f"{k}={v}" for k, v in row.items()))
    elif isinstance(result, dict):
        for key, value in result.items():
            print(f"{key}: {json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value}")
    elif result is not None:
        print(result)


def valid_identifier(name):
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""):
        raise SystemExit(f"Invalid SQL identifier: {name!r}")
    return name


def valid_column_spec(spec):
    if ":" not in spec:
        raise SystemExit("--column must be name:TYPE")
    name, typ = spec.split(":", 1)
    name = valid_identifier(name.strip())
    typ = typ.strip().upper()
    allowed = {"TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC", "JSON"}
    if typ not in allowed:
        raise SystemExit(f"Unsupported column type {typ!r}; use one of {', '.join(sorted(allowed))}")
    return name, "TEXT" if typ == "JSON" else typ


def ensure_source(conn, name, kind="manual", metadata=None):
    source_id = stable_id("src", name)
    conn.execute(
        "INSERT INTO sources (id, name, kind, metadata) VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, metadata=excluded.metadata",
        (source_id, name, kind, json_dumps(metadata or {})),
    )
    return conn.execute("SELECT id FROM sources WHERE name=?", (name,)).fetchone()["id"]


def parse_set_values(values):
    out = {}
    for value in values or []:
        if "=" not in value:
            raise SystemExit("--set must be key=value")
        key, val = value.split("=", 1)
        out[valid_identifier(key)] = val
    return out


def cmd_init(conn, args):
    init_schema(conn)
    conn.commit()
    return {"ok": True, "dbPath": str(resolved_db_path(args)), "schemaVersion": schema_version(conn)}


def cmd_stats(conn, args):
    init_schema(conn)
    return {
        "dbPath": str(resolved_db_path(args)),
        "schemaVersion": schema_version(conn),
        "ftsAvailable": fts_available(conn),
        "sources": conn.execute("SELECT count(*) AS count FROM sources").fetchone()["count"],
        "entities": rows_to_dicts(conn.execute("SELECT type, count(*) AS count FROM entities GROUP BY type ORDER BY type")),
        "library": dict(conn.execute("SELECT count(*) AS total, sum(read) AS read, sum(in_queue) AS in_queue, coalesce(sum(highlight_count), 0) AS highlight_count FROM library_items").fetchone()),
        "digests": dict(conn.execute("SELECT count(*) AS total, sum(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent FROM digests").fetchone()),
        "tables": len(user_table_names(conn)),
    }


def cmd_table_list(conn, args):
    return [{"name": name, "core": name in CORE_TABLES} for name in user_table_names(conn)]


def cmd_table_describe(conn, args):
    table = valid_identifier(args.table)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        raise SystemExit(f"Table does not exist: {table}")
    return {"table": table, "columns": rows_to_dicts(conn.execute(f"PRAGMA table_info({table})")), "indexes": rows_to_dicts(conn.execute(f"PRAGMA index_list({table})"))}


def cmd_table_create(conn, args):
    table = valid_identifier(args.table)
    columns = [valid_column_spec(spec) for spec in (args.column or [])]
    if not columns:
        raise SystemExit("table create requires at least one --column name:TYPE")
    parts = ["id TEXT PRIMARY KEY", "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))", "updated_at TEXT"]
    for name, typ in columns:
        if name in {"id", "created_at", "updated_at"}:
            raise SystemExit(f"{name} is managed automatically")
        parts.append(f"{name} {typ}")
    sql = f"CREATE TABLE {'IF NOT EXISTS ' if args.if_not_exists else ''}{table} ({', '.join(parts)})"
    if args.dry_run:
        return {"dryRun": True, "sql": sql}
    conn.execute(sql)
    conn.commit()
    return {"created": table, "columns": [name for name, _ in columns]}


def cmd_table_add_column(conn, args):
    table = valid_identifier(args.table)
    name, typ = valid_column_spec(args.column)
    sql = f"ALTER TABLE {table} ADD COLUMN {name} {typ}"
    if args.dry_run:
        return {"dryRun": True, "sql": sql}
    conn.execute(sql)
    conn.commit()
    return {"table": table, "addedColumn": name, "type": typ}


def cmd_table_drop(conn, args):
    table = valid_identifier(args.table)
    if table in CORE_TABLES or is_fts_shadow_table(table):
        raise SystemExit(f"Refusing to drop core table: {table}")
    if not args.force:
        raise SystemExit("table drop requires --force")
    if args.dry_run:
        return {"dryRun": True, "sql": f"DROP TABLE {table}"}
    conn.execute(f"DROP TABLE {table}")
    conn.commit()
    return {"dropped": table}


def cmd_doctor(conn, args):
    init_schema(conn)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_key_rows = rows_to_dicts(conn.execute("PRAGMA foreign_key_check"))
    table_counts = {}
    for name in user_table_names(conn):
        if name in FTS_TABLES:
            continue
        table_counts[name] = conn.execute(f"SELECT count(*) AS count FROM {name}").fetchone()["count"]
    fts_tables = {
        name: bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())
        for name in sorted(FTS_TABLES)
    }
    result = {
        "ok": integrity == "ok" and not foreign_key_rows and schema_version(conn) == SCHEMA_VERSION,
        "dbPath": str(resolved_db_path(args)),
        "schemaVersion": schema_version(conn),
        "expectedSchemaVersion": SCHEMA_VERSION,
        "journalMode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
        "busyTimeoutMs": conn.execute("PRAGMA busy_timeout").fetchone()[0],
        "foreignKeys": bool(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
        "integrityCheck": integrity,
        "foreignKeyErrors": foreign_key_rows,
        "ftsAvailable": fts_available(conn),
        "ftsTables": fts_tables,
        "tableCounts": table_counts,
    }
    return result


def cmd_checkpoint(conn, args):
    init_schema(conn)
    conn.commit()
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return {"checkpoint": {"busy": row[0], "logFrames": row[1], "checkpointedFrames": row[2]}}


def cmd_optimize(conn, args):
    init_schema(conn)
    conn.execute("PRAGMA optimize")
    if ensure_fts_schema(conn):
        for table in sorted(FTS_TABLES):
            conn.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
    conn.commit()
    return {"optimized": True}


def cmd_fts_status(conn, args):
    init_schema(conn)
    return {
        "available": fts_available(conn),
        "tables": {
            name: bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())
            for name in sorted(FTS_TABLES)
        },
    }


def cmd_fts_rebuild(conn, args):
    init_schema(conn)
    result = rebuild_fts(conn)
    conn.commit()
    return result


def table_columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def cmd_row_add(conn, args):
    table = valid_identifier(args.table)
    known_columns = table_columns(conn, table)
    values = parse_set_values(args.set)
    row_id = args.id or stable_id("row", f"{table}:{json_dumps(values)}:{now_iso()}")
    if "id" in known_columns:
        values = {"id": row_id, **values}
    if "updated_at" in known_columns:
        values["updated_at"] = now_iso()
    columns = list(values.keys())
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
    if args.dry_run:
        return {"dryRun": True, "sql": sql, "values": values}
    conn.execute(sql, [values[column] for column in columns])
    conn.commit()
    return {"inserted": row_id, "table": table}


def cmd_row_list(conn, args):
    table = valid_identifier(args.table)
    limit = min(max(args.limit, 1), 500)
    where = ""
    params = []
    if args.where:
        if "=" not in args.where:
            raise SystemExit("--where must be key=value")
        column, value = args.where.split("=", 1)
        where = f" WHERE {valid_identifier(column)} = ?"
        params.append(value)
    return rows_to_dicts(conn.execute(f"SELECT * FROM {table}{where} LIMIT ?", params + [limit]))


def cmd_row_get(conn, args):
    table = valid_identifier(args.table)
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (args.id,)).fetchone()
    if not row:
        raise SystemExit(f"Row not found: {args.id}")
    return dict(row)


def cmd_row_update(conn, args):
    table = valid_identifier(args.table)
    known_columns = table_columns(conn, table)
    values = parse_set_values(args.set)
    if not values:
        raise SystemExit("row update requires --set key=value")
    if "updated_at" in known_columns:
        values["updated_at"] = now_iso()
    sql = f"UPDATE {table} SET {', '.join(f'{key}=?' for key in values)} WHERE id=?"
    if args.dry_run:
        return {"dryRun": True, "sql": sql, "values": values}
    cur = conn.execute(sql, list(values.values()) + [args.id])
    conn.commit()
    return {"updated": cur.rowcount, "id": args.id, "table": table}


def cmd_row_delete(conn, args):
    table = valid_identifier(args.table)
    if not args.force:
        raise SystemExit("row delete requires --force")
    if args.dry_run:
        return {"dryRun": True, "sql": f"DELETE FROM {table} WHERE id=?", "id": args.id}
    cur = conn.execute(f"DELETE FROM {table} WHERE id=?", (args.id,))
    conn.commit()
    return {"deleted": cur.rowcount, "id": args.id, "table": table}


def cmd_entity_add(conn, args):
    init_schema(conn)
    key = canonical_key(args.type, args.title, args.url, args.key)
    entity_id = args.id or stable_id("ent", f"{args.type}:{key}")
    conn.execute(
        "INSERT INTO entities (id, type, canonical_key, title, subtitle, url, metadata, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(type, canonical_key) DO UPDATE SET title=excluded.title, subtitle=excluded.subtitle, url=excluded.url, metadata=excluded.metadata, updated_at=excluded.updated_at",
        (entity_id, args.type, key, args.title, args.subtitle, args.url, json_dumps(parse_json_object(args.metadata)), now_iso()),
    )
    row = conn.execute("SELECT * FROM entities WHERE type=? AND canonical_key=?", (args.type, key)).fetchone()
    rebuild_fts(conn)
    conn.commit()
    return dict(row)


def cmd_entity_search(conn, args):
    init_schema(conn)
    query = fts_query(args.query)
    if query and ensure_fts_schema(conn):
        params = [query]
        type_clause = ""
        if args.type:
            type_clause = " AND entities.type=?"
            params.append(args.type)
        try:
            return rows_to_dicts(conn.execute(
                f"SELECT entities.* FROM entities_fts JOIN entities ON entities.id = entities_fts.entity_id WHERE entities_fts MATCH ?{type_clause} ORDER BY bm25(entities_fts) LIMIT ?",
                params + [args.limit],
            ))
        except sqlite3.Error:
            pass
    term = f"%{args.query}%"
    params = [term, term, term]
    type_clause = ""
    if args.type:
        type_clause = " AND type=?"
        params.append(args.type)
    return rows_to_dicts(conn.execute(f"SELECT * FROM entities WHERE (title LIKE ? OR subtitle LIKE ? OR url LIKE ?){type_clause} ORDER BY updated_at DESC LIMIT ?", params + [args.limit]))


def cmd_entity_get(conn, args):
    row = conn.execute("SELECT * FROM entities WHERE id=?", (args.id,)).fetchone()
    if not row:
        raise SystemExit(f"Entity not found: {args.id}")
    return {
        "entity": dict(row),
        "facts": rows_to_dicts(conn.execute("SELECT key, value, confidence, observed_at FROM facts WHERE entity_id=? ORDER BY created_at DESC", (args.id,))),
        "tags": [r["tag"] for r in conn.execute("SELECT tag FROM tags WHERE entity_id=? ORDER BY tag", (args.id,))],
        "events": rows_to_dicts(conn.execute("SELECT type, occurred_at, metadata FROM events WHERE entity_id=? ORDER BY occurred_at DESC, created_at DESC LIMIT 20", (args.id,))),
    }


def cmd_entity_delete(conn, args):
    if not args.force:
        raise SystemExit("entity delete requires --force")
    cur = conn.execute("DELETE FROM entities WHERE id=?", (args.id,))
    rebuild_fts(conn)
    conn.commit()
    return {"deleted": cur.rowcount, "id": args.id}


def cmd_fact_add(conn, args):
    init_schema(conn)
    source_id = ensure_source(conn, args.source, "manual") if args.source else None
    fact_id = args.id or stable_id("fact", f"{args.entity_id}:{args.key}:{args.value}:{args.observed_at or now_iso()}")
    conn.execute("INSERT INTO facts (id, entity_id, key, value, source_id, confidence, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (fact_id, args.entity_id, args.key, args.value, source_id, args.confidence, args.observed_at))
    conn.commit()
    return {"inserted": fact_id}


def cmd_fact_list(conn, args):
    params = []
    where = []
    if args.entity_id:
        where.append("entity_id=?")
        params.append(args.entity_id)
    if args.key:
        where.append("key=?")
        params.append(args.key)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return rows_to_dicts(conn.execute(f"SELECT * FROM facts{clause} ORDER BY created_at DESC LIMIT ?", params + [args.limit]))


def cmd_fact_delete(conn, args):
    if not args.force:
        raise SystemExit("fact delete requires --force")
    cur = conn.execute("DELETE FROM facts WHERE id=?", (args.id,))
    conn.commit()
    return {"deleted": cur.rowcount, "id": args.id}


def cmd_event_add(conn, args):
    init_schema(conn)
    source_id = ensure_source(conn, args.source, "manual") if args.source else None
    event_id = args.id or stable_id("evt", f"{args.entity_id}:{args.type}:{args.occurred_at or now_iso()}:{args.metadata}")
    conn.execute("INSERT INTO events (id, entity_id, source_id, type, occurred_at, metadata) VALUES (?, ?, ?, ?, ?, ?)", (event_id, args.entity_id, source_id, args.type, args.occurred_at, json_dumps(parse_json_object(args.metadata))))
    conn.commit()
    return {"inserted": event_id}


def cmd_event_list(conn, args):
    params = []
    where = []
    if args.entity_id:
        where.append("entity_id=?")
        params.append(args.entity_id)
    if args.type:
        where.append("type=?")
        params.append(args.type)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return rows_to_dicts(conn.execute(f"SELECT * FROM events{clause} ORDER BY occurred_at DESC, created_at DESC LIMIT ?", params + [args.limit]))


def cmd_link_add(conn, args):
    init_schema(conn)
    metadata = parse_json_object(args.metadata)
    link_id = args.id or stable_id("link", f"{args.from_entity_id}:{args.to_entity_id}:{args.type}")
    conn.execute(
        "INSERT INTO links (id, from_entity_id, to_entity_id, type, metadata) VALUES (?, ?, ?, ?, ?) ON CONFLICT(from_entity_id, to_entity_id, type) DO UPDATE SET metadata=excluded.metadata",
        (link_id, args.from_entity_id, args.to_entity_id, args.type, json_dumps(metadata)),
    )
    row = conn.execute("SELECT * FROM links WHERE from_entity_id=? AND to_entity_id=? AND type=?", (args.from_entity_id, args.to_entity_id, args.type)).fetchone()
    conn.commit()
    return dict(row)


def cmd_link_list(conn, args):
    params = []
    where = []
    if args.entity_id:
        where.append("(from_entity_id=? OR to_entity_id=?)")
        params.extend([args.entity_id, args.entity_id])
    if args.type:
        where.append("type=?")
        params.append(args.type)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return rows_to_dicts(conn.execute(f"SELECT * FROM links{clause} ORDER BY created_at DESC LIMIT ?", params + [args.limit]))


def cmd_link_delete(conn, args):
    if not args.force:
        raise SystemExit("link delete requires --force")
    cur = conn.execute("DELETE FROM links WHERE id=?", (args.id,))
    conn.commit()
    return {"deleted": cur.rowcount, "id": args.id}


def cmd_tag_add(conn, args):
    init_schema(conn)
    source_id = ensure_source(conn, args.source, "manual") if args.source else None
    conn.execute("INSERT OR IGNORE INTO tags (entity_id, tag, source_id) VALUES (?, ?, ?)", (args.entity_id, args.tag, source_id))
    conn.commit()
    return {"tagged": args.entity_id, "tag": args.tag}


def cmd_tag_remove(conn, args):
    if not args.force:
        raise SystemExit("tag remove requires --force")
    cur = conn.execute("DELETE FROM tags WHERE entity_id=? AND tag=?", (args.entity_id, args.tag))
    conn.commit()
    return {"removed": cur.rowcount, "entityId": args.entity_id, "tag": args.tag}


def cmd_tag_list(conn, args):
    if args.entity_id:
        return rows_to_dicts(conn.execute("SELECT tag, created_at FROM tags WHERE entity_id=? ORDER BY tag", (args.entity_id,)))
    return rows_to_dicts(conn.execute("SELECT tag, count(*) AS count FROM tags GROUP BY tag ORDER BY count DESC, tag LIMIT ?", (args.limit,)))


def cmd_text_add(conn, args):
    init_schema(conn)
    source_id = ensure_source(conn, args.source or "manual", "manual")
    existing = conn.execute("SELECT coalesce(max(chunk_index), -1) AS max_index FROM text_chunks WHERE entity_id=?", (args.entity_id,)).fetchone()
    index = args.index if args.index is not None else int(existing["max_index"]) + 1
    chunk_id = args.id or stable_id("chunk", f"{args.entity_id}:{source_id}:{index}:{args.text}")
    conn.execute("INSERT INTO text_chunks (id, entity_id, source_id, chunk_index, chunk_text, metadata) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(entity_id, source_id, chunk_index) DO UPDATE SET chunk_text=excluded.chunk_text, metadata=excluded.metadata", (chunk_id, args.entity_id, source_id, index, args.text, json_dumps(parse_json_object(args.metadata))))
    rebuild_fts(conn)
    conn.commit()
    return {"inserted": chunk_id, "entityId": args.entity_id, "chunkIndex": index}


def cmd_text_search(conn, args):
    init_schema(conn)
    query = fts_query(args.query)
    if query and ensure_fts_schema(conn):
        try:
            return rows_to_dicts(conn.execute(
                "SELECT text_chunks.id, text_chunks.entity_id, entities.title, text_chunks.chunk_index, substr(text_chunks.chunk_text, 1, ?) AS snippet FROM text_chunks_fts JOIN text_chunks ON text_chunks.id = text_chunks_fts.chunk_id JOIN entities ON entities.id = text_chunks.entity_id WHERE text_chunks_fts MATCH ? ORDER BY bm25(text_chunks_fts) LIMIT ?",
                (args.snippet, query, args.limit),
            ))
        except sqlite3.Error:
            pass
    term = f"%{args.query}%"
    return rows_to_dicts(conn.execute("SELECT text_chunks.id, text_chunks.entity_id, entities.title, text_chunks.chunk_index, substr(text_chunks.chunk_text, 1, ?) AS snippet FROM text_chunks JOIN entities ON entities.id = text_chunks.entity_id WHERE text_chunks.chunk_text LIKE ? ORDER BY text_chunks.created_at DESC LIMIT ?", (args.snippet, term, args.limit)))


def cmd_library_add(conn, args):
    init_schema(conn)
    source_id = ensure_source(conn, args.source, "manual_library")
    title = args.title or title_from_url(args.url)
    normalized = normalize_url(args.url)
    key = canonical_key("library_item", title, args.url)
    entity_id = stable_id("ent", "library:" + key)
    existing = None
    if normalized:
        existing = conn.execute("SELECT id, entity_id FROM library_items WHERE normalized_url=? LIMIT 1", (normalized,)).fetchone()
    item_id = args.id or (existing["id"] if existing else stable_id("read", "library:" + key))
    entity_id = existing["entity_id"] if existing else entity_id
    conn.execute("INSERT INTO entities (id, type, canonical_key, title, subtitle, url, metadata, updated_at) VALUES (?, 'library_item', ?, ?, ?, ?, ?, ?) ON CONFLICT(type, canonical_key) DO UPDATE SET title=excluded.title, subtitle=excluded.subtitle, url=excluded.url, updated_at=excluded.updated_at", (entity_id, key, title, args.author, normalized or args.url, json_dumps({"source": args.source}), now_iso()))
    conn.execute(
        "INSERT INTO library_items (id, entity_id, source_id, title, author, publisher, url, normalized_url, tags, in_queue, read, status, metadata, last_interaction_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, author=excluded.author, publisher=excluded.publisher, url=excluded.url, normalized_url=excluded.normalized_url, tags=excluded.tags, in_queue=excluded.in_queue, read=excluded.read, status=excluded.status, metadata=excluded.metadata, last_interaction_at=excluded.last_interaction_at, updated_at=excluded.updated_at",
        (item_id, entity_id, source_id, title, args.author, args.publisher, args.url, normalized, json_dumps(args.tag or []), int(args.queue), int(args.read), args.status, json_dumps(parse_json_object(args.metadata)), now_iso(), now_iso()),
    )
    for tag in args.tag or []:
        conn.execute("INSERT OR IGNORE INTO tags (entity_id, tag, source_id) VALUES (?, ?, ?)", (entity_id, tag, source_id))
    rebuild_fts(conn)
    conn.commit()
    return {"id": item_id, "entityId": entity_id, "title": title, "url": args.url}


def cmd_library_queue(conn, args):
    args.queue = True
    args.read = False
    args.status = "queued_for_digest"
    if not args.tag:
        args.tag = ["digest-queue"]
    elif "digest-queue" not in args.tag:
        args.tag.append("digest-queue")
    return cmd_library_add(conn, args)


def cmd_library_search(conn, args):
    init_schema(conn)
    query = fts_query(args.query)
    if query and ensure_fts_schema(conn):
        try:
            return rows_to_dicts(conn.execute(
                "SELECT library_items.id, library_items.entity_id, library_items.title, library_items.author, library_items.publisher, library_items.url, library_items.status, library_items.in_queue, library_items.read, library_items.last_interaction_at FROM library_items_fts JOIN library_items ON library_items.id = library_items_fts.library_item_id WHERE library_items_fts MATCH ? ORDER BY bm25(library_items_fts) LIMIT ?",
                (query, args.limit),
            ))
        except sqlite3.Error:
            pass
    term = f"%{args.query}%"
    return rows_to_dicts(conn.execute("SELECT id, entity_id, title, author, publisher, url, status, in_queue, read, last_interaction_at FROM library_items WHERE title LIKE ? OR author LIKE ? OR publisher LIKE ? OR url LIKE ? ORDER BY last_interaction_at DESC, updated_at DESC LIMIT ?", (term, term, term, term, args.limit)))


def cmd_library_list(conn, args):
    params = []
    where = []
    if args.status:
        where.append("status=?")
        params.append(args.status)
    if args.queue:
        where.append("in_queue=1")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return rows_to_dicts(conn.execute(f"SELECT id, title, author, publisher, url, status, in_queue, read, last_interaction_at FROM library_items{clause} ORDER BY last_interaction_at DESC, updated_at DESC LIMIT ?", params + [args.limit]))


def cmd_sql(conn, args):
    is_write = bool(WRITE_SQL_RE.search(args.sql))
    if is_write and not (args.write and args.force):
        raise SystemExit("sql is read-only by default; writes require --write --force")
    if args.dry_run:
        return {"dryRun": True, "sql": args.sql, "write": is_write}
    cur = conn.execute(args.sql)
    if is_write:
        conn.commit()
        return {"changed": cur.rowcount}
    return rows_to_dicts(cur.fetchall())


def build_parser():
    parser = argparse.ArgumentParser(prog="personal-db", description="Local-first personal SQLite database CLI for OpenClaw agents.")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--json", action="store_true", help="print JSON output")
    parser.add_argument("--dry-run", action="store_true", help="preview writes without changing data")
    parser.add_argument("--force", action="store_true", help="confirm destructive operation")
    parser.add_argument("--version", action="version", version="personal-db 0.2.0")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_force_arg(p):
        p.add_argument("--force", action="store_true", default=argparse.SUPPRESS, help="confirm destructive operation")

    def add_dry_run_arg(p):
        p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS, help="preview writes without changing data")

    sub.add_parser("init", help="initialize core schema").set_defaults(func=cmd_init)
    sub.add_parser("stats", help="show database stats").set_defaults(func=cmd_stats)
    sub.add_parser("doctor", help="check schema, pragmas, integrity, and search indexes").set_defaults(func=cmd_doctor)
    sub.add_parser("checkpoint", help="checkpoint and truncate the WAL file").set_defaults(func=cmd_checkpoint)
    sub.add_parser("optimize", help="run SQLite and FTS optimize maintenance").set_defaults(func=cmd_optimize)

    fts = sub.add_parser("fts", help="manage full-text search indexes").add_subparsers(dest="fts_command", required=True)
    fts.add_parser("status").set_defaults(func=cmd_fts_status)
    fts.add_parser("rebuild").set_defaults(func=cmd_fts_rebuild)

    table = sub.add_parser("table", help="manage tables").add_subparsers(dest="table_command", required=True)
    table.add_parser("list").set_defaults(func=cmd_table_list)
    p = table.add_parser("describe"); p.add_argument("table"); p.set_defaults(func=cmd_table_describe)
    p = table.add_parser("create"); p.add_argument("table"); p.add_argument("--column", action="append"); p.add_argument("--if-not-exists", action="store_true"); add_dry_run_arg(p); p.set_defaults(func=cmd_table_create)
    p = table.add_parser("add-column"); p.add_argument("table"); p.add_argument("--column", required=True); add_dry_run_arg(p); p.set_defaults(func=cmd_table_add_column)
    p = table.add_parser("drop"); p.add_argument("table"); add_force_arg(p); add_dry_run_arg(p); p.set_defaults(func=cmd_table_drop)

    row = sub.add_parser("row", help="manage arbitrary rows").add_subparsers(dest="row_command", required=True)
    p = row.add_parser("add"); p.add_argument("table"); p.add_argument("--id"); p.add_argument("--set", action="append"); add_dry_run_arg(p); p.set_defaults(func=cmd_row_add)
    p = row.add_parser("list"); p.add_argument("table"); p.add_argument("--where"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_row_list)
    p = row.add_parser("get"); p.add_argument("table"); p.add_argument("id"); p.set_defaults(func=cmd_row_get)
    p = row.add_parser("update"); p.add_argument("table"); p.add_argument("id"); p.add_argument("--set", action="append"); add_dry_run_arg(p); p.set_defaults(func=cmd_row_update)
    p = row.add_parser("delete"); p.add_argument("table"); p.add_argument("id"); add_force_arg(p); add_dry_run_arg(p); p.set_defaults(func=cmd_row_delete)

    entity = sub.add_parser("entity", help="manage entities").add_subparsers(dest="entity_command", required=True)
    p = entity.add_parser("add"); p.add_argument("--id"); p.add_argument("--type", required=True); p.add_argument("--title", required=True); p.add_argument("--key"); p.add_argument("--subtitle"); p.add_argument("--url"); p.add_argument("--metadata"); p.set_defaults(func=cmd_entity_add)
    p = entity.add_parser("search"); p.add_argument("query"); p.add_argument("--type"); p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_entity_search)
    p = entity.add_parser("get"); p.add_argument("id"); p.set_defaults(func=cmd_entity_get)
    p = entity.add_parser("delete"); p.add_argument("id"); add_force_arg(p); p.set_defaults(func=cmd_entity_delete)

    fact = sub.add_parser("fact", help="manage facts").add_subparsers(dest="fact_command", required=True)
    p = fact.add_parser("add"); p.add_argument("entity_id"); p.add_argument("key"); p.add_argument("value"); p.add_argument("--id"); p.add_argument("--source"); p.add_argument("--confidence", type=float, default=1.0); p.add_argument("--observed-at"); p.set_defaults(func=cmd_fact_add)
    p = fact.add_parser("list"); p.add_argument("--entity-id"); p.add_argument("--key"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_fact_list)
    p = fact.add_parser("delete"); p.add_argument("id"); add_force_arg(p); p.set_defaults(func=cmd_fact_delete)

    event = sub.add_parser("event", help="manage events").add_subparsers(dest="event_command", required=True)
    p = event.add_parser("add"); p.add_argument("entity_id"); p.add_argument("type"); p.add_argument("--id"); p.add_argument("--source"); p.add_argument("--occurred-at"); p.add_argument("--metadata"); p.set_defaults(func=cmd_event_add)
    p = event.add_parser("list"); p.add_argument("--entity-id"); p.add_argument("--type"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_event_list)

    link = sub.add_parser("link", help="manage entity links").add_subparsers(dest="link_command", required=True)
    p = link.add_parser("add"); p.add_argument("from_entity_id"); p.add_argument("to_entity_id"); p.add_argument("type"); p.add_argument("--id"); p.add_argument("--metadata"); p.set_defaults(func=cmd_link_add)
    p = link.add_parser("list"); p.add_argument("--entity-id"); p.add_argument("--type"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_link_list)
    p = link.add_parser("delete"); p.add_argument("id"); add_force_arg(p); p.set_defaults(func=cmd_link_delete)

    tag = sub.add_parser("tag", help="manage tags").add_subparsers(dest="tag_command", required=True)
    p = tag.add_parser("add"); p.add_argument("entity_id"); p.add_argument("tag"); p.add_argument("--source"); p.set_defaults(func=cmd_tag_add)
    p = tag.add_parser("remove"); p.add_argument("entity_id"); p.add_argument("tag"); add_force_arg(p); p.set_defaults(func=cmd_tag_remove)
    p = tag.add_parser("list"); p.add_argument("--entity-id"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_tag_list)

    text = sub.add_parser("text", help="manage searchable text chunks").add_subparsers(dest="text_command", required=True)
    p = text.add_parser("add"); p.add_argument("entity_id"); p.add_argument("--text", required=True); p.add_argument("--id"); p.add_argument("--source"); p.add_argument("--index", type=int); p.add_argument("--metadata"); p.set_defaults(func=cmd_text_add)
    p = text.add_parser("search"); p.add_argument("query"); p.add_argument("--limit", type=int, default=20); p.add_argument("--snippet", type=int, default=240); p.set_defaults(func=cmd_text_search)

    library = sub.add_parser("library", help="manage reading library items").add_subparsers(dest="library_command", required=True)
    for name, func in [("add", cmd_library_add), ("queue", cmd_library_queue)]:
        p = library.add_parser(name)
        p.add_argument("url")
        p.add_argument("--id")
        p.add_argument("--title")
        p.add_argument("--author")
        p.add_argument("--publisher")
        p.add_argument("--source", default="manual")
        p.add_argument("--tag", action="append")
        p.add_argument("--metadata")
        p.add_argument("--read", action="store_true")
        p.add_argument("--queue", action="store_true")
        p.add_argument("--status", default="saved")
        p.set_defaults(func=func)
    p = library.add_parser("search"); p.add_argument("query"); p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_library_search)
    p = library.add_parser("list"); p.add_argument("--status"); p.add_argument("--queue", action="store_true"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_library_list)

    p = sub.add_parser("sql", help="run SQL; read-only unless --write --force")
    p.add_argument("sql")
    p.add_argument("--write", action="store_true")
    add_force_arg(p)
    add_dry_run_arg(p)
    p.set_defaults(func=cmd_sql)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    conn = connect(args)
    try:
        result = args.func(conn, args)
        print_result(args, result)
    except sqlite3.Error as exc:
        raise SystemExit(f"sqlite error: {exc}") from exc
    finally:
        conn.close()


if __name__ == "__main__":
    main()
