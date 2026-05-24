import json
import subprocess
import sys
import tempfile
import unittest
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "personal_db.py"


class PersonalDbCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, check=True):
        result = subprocess.run(
            [sys.executable, str(CLI), "--db", str(self.db), "--json", *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        if check and result.returncode != 0:
            self.fail(f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result

    def json_cli(self, *args):
        result = self.run_cli(*args)
        return json.loads(result.stdout)

    def test_entity_fact_event_tag_text_flow(self):
        initialized = self.json_cli("init")
        self.assertEqual(initialized["schemaVersion"], 2)
        entity = self.json_cli("entity", "add", "--type", "hike", "--title", "Mount Si")
        entity_id = entity["id"]

        fact = self.json_cli("fact", "add", entity_id, "difficulty", "moderate")
        event = self.json_cli("event", "add", entity_id, "completed", "--occurred-at", "2026-05-24")
        tag = self.json_cli("tag", "add", entity_id, "seattle")
        text = self.json_cli("text", "add", entity_id, "--index", "0", "--text", "Steep but straightforward.")

        self.assertTrue(fact["inserted"].startswith("fact_"))
        self.assertTrue(event["inserted"].startswith("evt_"))
        self.assertEqual(tag["tag"], "seattle")
        self.assertEqual(text["chunkIndex"], 0)

        fetched = self.json_cli("entity", "get", entity_id)
        self.assertEqual(fetched["entity"]["title"], "Mount Si")
        self.assertEqual(fetched["facts"][0]["key"], "difficulty")
        self.assertIn("seattle", fetched["tags"])

    def test_doctor_fts_and_maintenance_commands(self):
        entity = self.json_cli("entity", "add", "--type", "hike", "--title", "Enchantments")
        self.json_cli("text", "add", entity["id"], "--text", "Alpine lakes and strict permit planning.")
        self.json_cli("library", "add", "https://example.com/alpine-lakes", "--title", "Alpine Lakes Guide")

        entity_rows = self.json_cli("entity", "search", "Enchantments")
        text_rows = self.json_cli("text", "search", "permit")
        library_rows = self.json_cli("library", "search", "Alpine")
        self.assertEqual(entity_rows[0]["id"], entity["id"])
        self.assertEqual(text_rows[0]["entity_id"], entity["id"])
        self.assertEqual(library_rows[0]["title"], "Alpine Lakes Guide")

        status = self.json_cli("fts", "status")
        self.assertTrue(status["available"])
        self.assertTrue(all(status["tables"].values()))
        self.assertTrue(self.json_cli("fts", "rebuild")["rebuilt"])

        doctor = self.json_cli("doctor")
        self.assertTrue(doctor["ok"])
        self.assertEqual(doctor["schemaVersion"], 2)
        self.assertEqual(doctor["journalMode"], "wal")
        self.assertEqual(doctor["busyTimeoutMs"], 5000)
        self.assertTrue(doctor["foreignKeys"])
        self.assertEqual(doctor["integrityCheck"], "ok")

        self.assertTrue(self.json_cli("optimize")["optimized"])
        self.assertIn("checkpoint", self.json_cli("checkpoint"))

    def test_rejects_newer_schema_version(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version = 999")
        result = self.run_cli("init", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("newer than this CLI supports", result.stderr)

    def test_text_add_without_source_upserts_same_index(self):
        entity = self.json_cli("entity", "add", "--type", "hike", "--title", "Mailbox Peak")
        entity_id = entity["id"]
        self.json_cli("text", "add", entity_id, "--index", "0", "--text", "first")
        self.json_cli("text", "add", entity_id, "--index", "0", "--text", "second")
        rows = self.json_cli("sql", "select count(*) as n, max(chunk_text) as text from text_chunks")
        self.assertEqual(rows[0]["n"], 1)
        self.assertEqual(rows[0]["text"], "second")

    def test_link_commands(self):
        a = self.json_cli("entity", "add", "--type", "hike", "--title", "Hike A")
        b = self.json_cli("entity", "add", "--type", "place", "--title", "Place B")
        link = self.json_cli("link", "add", a["id"], b["id"], "nearby")
        self.assertEqual(link["type"], "nearby")
        links = self.json_cli("link", "list", "--entity-id", a["id"])
        self.assertEqual(len(links), 1)
        deleted = self.json_cli("link", "delete", link["id"], "--force")
        self.assertEqual(deleted["deleted"], 1)

    def test_custom_table_row_flow_and_bad_where_error(self):
        self.json_cli("table", "create", "hike_logs", "--column", "entity_id:TEXT", "--column", "date:TEXT")
        inserted = self.json_cli("row", "add", "hike_logs", "--set", "entity_id=ent_test", "--set", "date=2026-05-24")
        rows = self.json_cli("row", "list", "hike_logs", "--where", "entity_id=ent_test")
        self.assertEqual(rows[0]["id"], inserted["inserted"])

        bad = self.run_cli("row", "list", "hike_logs", "--where", "badwhere", check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("--where must be key=value", bad.stderr)

    def test_sql_write_requires_force(self):
        self.json_cli("init")
        denied = self.run_cli("sql", "delete from entities", "--write", check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("--write --force", denied.stderr)

    def test_library_queue_dedupes_normalized_url(self):
        self.json_cli("library", "add", "https://example.com/post?utm_source=x", "--title", "First")
        queued = self.json_cli("library", "queue", "https://example.com/post", "--title", "Second")
        rows = self.json_cli("library", "search", "example")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], queued["id"])
        self.assertEqual(rows[0]["status"], "queued_for_digest")


if __name__ == "__main__":
    unittest.main()
