# Personal DB Plugin

Local-first personal SQLite database tooling for OpenClaw agents.

This plugin gives an OpenClaw agent a safe CLI for storing and querying personal data without sending it to a hosted service. It starts with a reading-library workflow and a generic data model that can expand into hikes, meals, workouts, restaurants, places, people, and other domains.

## What Ships

- .codex-plugin/plugin.json - OpenClaw plugin manifest
- openclaw.plugin.json - OpenClaw-native metadata for future plugin packaging
- skills/personal-database/SKILL.md - agent instructions
- scripts/personal_db.py - portable Python/SQLite CLI
- tests/ - temp-database smoke tests

The plugin contains tooling and schema only. It must not include anyone's actual database, imports, reading content, logs, memory files, or backups.

## Quick Start

~~~sh
python3 scripts/personal_db.py --json init
python3 scripts/personal_db.py --json stats
python3 scripts/personal_db.py --json doctor
python3 scripts/personal_db.py --json library queue "https://example.com/article" --title "Article title"
python3 scripts/personal_db.py --json entity add --type hike --title "Mount Si"
python3 scripts/personal_db.py --json entity search "Mount Si"
~~~

By default, the CLI uses:

1. --db <path>
2. PERSONAL_DATABASE_DB
3. PERSONAL_DATA_DB
4. <workspace>/personal-data/db/main.sqlite when available
5. ~/.local/share/openclaw-personal-database/main.sqlite

For automation, pass --json.

## Core Model

Use the generic tables first:

- entities: durable things like articles, hikes, people, restaurants, workouts
- facts: small attributes about entities
- events: time-based history
- links: relationships between entities
- tags: flexible grouping
- text_chunks: notes, extracted text, transcripts, descriptions

Create custom tables when you need repeated structured measurements, for example workout_sets, hike_logs, meal_items, or restaurant_visits.

## Storage Mechanics

- Schema changes are versioned with PRAGMA user_version and a migrations table.
- The CLI refuses to open databases created by a newer schema version.
- Connections use WAL mode, synchronous=NORMAL, foreign keys, and a 5s busy timeout.
- Search uses SQLite FTS5 indexes for entities, library items, and text chunks when available, with LIKE fallback.
- Use doctor to check integrity, schema version, WAL mode, foreign keys, and FTS index presence.

## Examples

~~~sh
entity_id=$(python3 scripts/personal_db.py --json entity add --type hike --title "Mount Si" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
python3 scripts/personal_db.py --json fact add "$entity_id" difficulty moderate
python3 scripts/personal_db.py --json event add "$entity_id" completed --occurred-at 2026-05-24
python3 scripts/personal_db.py --json tag add "$entity_id" seattle
python3 scripts/personal_db.py --json text add "$entity_id" --text "Steep but straightforward trail."

python3 scripts/personal_db.py --json table create hike_logs \
  --column entity_id:TEXT \
  --column date:TEXT \
  --column distance_miles:REAL \
  --column notes:TEXT

python3 scripts/personal_db.py --json row add hike_logs \
  --set entity_id="$entity_id" \
  --set date=2026-05-24 \
  --set distance_miles=6.2

python3 scripts/personal_db.py --json fts rebuild
python3 scripts/personal_db.py --json optimize
python3 scripts/personal_db.py --json checkpoint
~~~

## Safety

- Destructive operations require --force.
- Core tables cannot be dropped with table drop.
- Raw SQL is read-only by default. Writes require --write --force.
- doctor is the preferred pre-publish/pre-backup health check.
- Tests use temporary SQLite databases only.

## Test

~~~sh
python3 -m unittest discover -s tests
~~~
