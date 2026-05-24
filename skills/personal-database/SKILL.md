---
name: personal-database
description: Use when OpenClaw needs to create, read, update, delete, search, or organize a user's local personal SQLite database, including library items, reading queues, hikes, workouts, meals, events, facts, tags, text chunks, custom tables, and future personal-data domains.
---

# Personal Database

Use the bundled CLI at scripts/personal_db.py to operate on the user's local personal SQLite database. The plugin ships tooling and schema only; it must not include or publish the user's private main.sqlite or raw imports.

## Default Database

The CLI resolves the database in this order:

1. --db <path>
2. PERSONAL_DATABASE_DB
3. PERSONAL_DATA_DB
4. <workspace>/personal-data/db/main.sqlite when present, or when the workspace has a personal-data folder
5. ~/.local/share/openclaw-personal-database/main.sqlite

When working inside a project that has its own database, pass --db explicitly:

    python3 scripts/personal_db.py --db ./personal-data/db/main.sqlite <command>

## Operating Rules

- Prefer CLI commands over hand-written SQL for writes.
- Use --json for machine-readable output.
- Run init before first use or after adding the plugin to a new workspace.
- Use doctor before backups, migrations, or larger automated edits.
- Treat entities, facts, events, links, tags, and text_chunks as the shared core model.
- Add domain tables only when the core model becomes awkward for repeated structured data.
- Use row commands for custom tables created by this CLI. For core tables, prefer entity/fact/event/link/tag/text/library commands.
- Never publish or copy a user's actual SQLite DB, imports, article content, or logs into a plugin.
- Destructive commands require --force; do not pass it unless the user explicitly asked for delete/drop behavior.
- Raw sql is read-only by default. Use --write --force only for intentional migrations or emergency fixes.
- Search commands use SQLite FTS5 when available and fall back to LIKE matching. If search looks stale after bulk SQL writes, run fts rebuild.
- The database uses WAL mode and schema versioning. If the CLI reports a newer schema version, stop and update the plugin instead of forcing writes.

## Common Commands

    python3 plugins/personal-database/scripts/personal_db.py --json init
    python3 plugins/personal-database/scripts/personal_db.py --json stats
    python3 plugins/personal-database/scripts/personal_db.py --json doctor
    python3 plugins/personal-database/scripts/personal_db.py --json fts status
    python3 plugins/personal-database/scripts/personal_db.py --json fts rebuild
    python3 plugins/personal-database/scripts/personal_db.py --json optimize
    python3 plugins/personal-database/scripts/personal_db.py --json checkpoint
    python3 plugins/personal-database/scripts/personal_db.py --json table list
    python3 plugins/personal-database/scripts/personal_db.py --json table describe library_items
    python3 plugins/personal-database/scripts/personal_db.py --json entity add --type hike --title "Mount Si" --url "https://example.com"
    python3 plugins/personal-database/scripts/personal_db.py --json entity search "Mount Si"
    python3 plugins/personal-database/scripts/personal_db.py --json fact add <entity-id> difficulty moderate
    python3 plugins/personal-database/scripts/personal_db.py --json event add <entity-id> completed --occurred-at 2026-05-24
    python3 plugins/personal-database/scripts/personal_db.py --json link add <from-entity-id> <to-entity-id> visited_at
    python3 plugins/personal-database/scripts/personal_db.py --json tag add <entity-id> seattle
    python3 plugins/personal-database/scripts/personal_db.py --json text add <entity-id> --text "Notes about this item"
    python3 plugins/personal-database/scripts/personal_db.py --json text search "difficult scramble"
    python3 plugins/personal-database/scripts/personal_db.py --json library add "https://example.com/post" --title "Post title"
    python3 plugins/personal-database/scripts/personal_db.py --json library queue "https://example.com/post" --title "Post title"
    python3 plugins/personal-database/scripts/personal_db.py --json library search "automation"
    python3 plugins/personal-database/scripts/personal_db.py --json table create hike_logs --column "entity_id:TEXT" --column "date:TEXT" --column "distance_miles:REAL" --column "notes:TEXT"
    python3 plugins/personal-database/scripts/personal_db.py --json row add hike_logs --set entity_id=<entity-id> --set date=2026-05-24 --set distance_miles=6.2
    python3 plugins/personal-database/scripts/personal_db.py --json row list hike_logs --limit 20

## Suggested Domain Pattern

Start every new domain with an entity:

- type: broad noun such as library_item, hike, restaurant, workout, meal, person, place
- canonical_key: stable dedupe key, auto-generated from type/title/url when omitted
- facts: small attributes that change rarely
- events: time-based activity or history
- tags: flexible grouping
- text_chunks: notes, descriptions, extracted text, transcripts

Create a custom table when you need repeated structured measurements or many rows per entity, such as workout_sets, meal_items, hike_logs, or restaurant_visits.
