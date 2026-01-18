#!/usr/bin/env python3
"""
Build SQLite database from OmniFocus transaction files.

Parses the XML transaction files in omnifocus-data/ and builds a queryable
SQLite database matching the OmniFocus schema.

Usage:
    python build_db.py --data-dir omnifocus-data --output omnifocus.sqlite
    python build_db.py --full-rebuild  # Force rebuild from scratch
"""

import argparse
import plistlib
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# OmniFocus XML namespace
NS = "{http://www.omnigroup.com/namespace/OmniFocus/v2}"


def create_schema(conn: sqlite3.Connection) -> None:
    """Create database schema."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS Task (
            id TEXT PRIMARY KEY,
            name TEXT,
            parent_task TEXT,
            project_folder TEXT,
            context TEXT,
            inbox INTEGER DEFAULT 0,
            flagged INTEGER DEFAULT 0,
            date_added TEXT,
            date_modified TEXT,
            date_due TEXT,
            date_start TEXT,
            date_completed TEXT,
            estimated_minutes INTEGER,
            rank INTEGER,
            note TEXT,
            is_project INTEGER DEFAULT 0,
            project_status TEXT,
            sequential INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS Context (
            id TEXT PRIMARY KEY,
            name TEXT,
            parent TEXT,
            rank INTEGER,
            date_added TEXT,
            date_modified TEXT,
            deleted INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS Folder (
            id TEXT PRIMARY KEY,
            name TEXT,
            parent TEXT,
            rank INTEGER,
            date_added TEXT,
            date_modified TEXT,
            deleted INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS Perspective (
            id TEXT PRIMARY KEY,
            name TEXT,
            filter_rules TEXT,
            value_data BLOB,
            date_added TEXT,
            date_modified TEXT
        );

        CREATE TABLE IF NOT EXISTS ODOMetadata (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_task_parent ON Task(parent_task);
        CREATE INDEX IF NOT EXISTS idx_task_context ON Task(context);
        CREATE INDEX IF NOT EXISTS idx_task_folder ON Task(project_folder);
        CREATE INDEX IF NOT EXISTS idx_task_completed ON Task(date_completed);
        CREATE INDEX IF NOT EXISTS idx_context_parent ON Context(parent);
        CREATE INDEX IF NOT EXISTS idx_folder_parent ON Folder(parent);
    """)
    conn.commit()


def get_text(elem: ET.Element, tag: str) -> str | None:
    """Get text content of a child element."""
    child = elem.find(f"{NS}{tag}")
    if child is not None and child.text:
        return child.text.strip()
    return None


def get_bool(elem: ET.Element, tag: str) -> int:
    """Get boolean value (0/1) from child element."""
    text = get_text(elem, tag)
    return 1 if text == "true" else 0


def get_int(elem: ET.Element, tag: str) -> int | None:
    """Get integer value from child element."""
    text = get_text(elem, tag)
    if text:
        try:
            return int(text)
        except ValueError:
            return None
    return None


def parse_task(elem: ET.Element) -> dict:
    """Parse a task element into a dictionary."""
    data = {
        "id": elem.get("id"),
        "name": get_text(elem, "name"),
        "inbox": get_bool(elem, "inbox"),
        "flagged": get_bool(elem, "flagged"),
        "date_added": get_text(elem, "added"),
        "date_modified": get_text(elem, "modified"),
        "date_due": get_text(elem, "due"),
        "date_start": get_text(elem, "start"),
        "date_completed": get_text(elem, "completed"),
        "estimated_minutes": get_int(elem, "estimated-minutes"),
        "rank": get_int(elem, "rank"),
        "note": get_text(elem, "note"),
        "sequential": 1 if get_text(elem, "order") == "sequential" else 0,
    }

    # Parent task (for subtasks)
    parent_elem = elem.find(f"{NS}task")
    if parent_elem is not None:
        # Could be empty (no parent) or have idref
        idref = parent_elem.get("idref")
        if idref:
            data["parent_task"] = idref

    # Context
    context_elem = elem.find(f"{NS}context")
    if context_elem is not None:
        ctx_id = context_elem.get("id") or context_elem.get("idref")
        if ctx_id:
            data["context"] = ctx_id

    # Project info (if this task is a project)
    project_elem = elem.find(f"{NS}project")
    if project_elem is not None:
        data["is_project"] = 1
        data["project_status"] = get_text(project_elem, "status")

        # Folder reference
        folder_elem = project_elem.find(f"{NS}folder")
        if folder_elem is not None:
            folder_id = folder_elem.get("idref")
            if folder_id:
                data["project_folder"] = folder_id

    return data


def parse_context(elem: ET.Element) -> dict:
    """Parse a context element into a dictionary."""
    data = {
        "id": elem.get("id"),
        "name": get_text(elem, "name"),
        "parent": None,
        "rank": get_int(elem, "rank"),
        "date_added": get_text(elem, "added"),
        "date_modified": get_text(elem, "modified"),
    }

    # Parent context
    parent_elem = elem.find(f"{NS}context")
    if parent_elem is not None:
        idref = parent_elem.get("idref")
        if idref:
            data["parent"] = idref

    return data


def parse_folder(elem: ET.Element) -> dict:
    """Parse a folder element into a dictionary."""
    data = {
        "id": elem.get("id"),
        "name": get_text(elem, "name"),
        "parent": None,
        "rank": get_int(elem, "rank"),
        "date_added": get_text(elem, "added"),
        "date_modified": get_text(elem, "modified"),
    }

    # Parent folder
    parent_elem = elem.find(f"{NS}folder")
    if parent_elem is not None:
        idref = parent_elem.get("idref")
        if idref:
            data["parent"] = idref

    return data


def parse_perspective(elem: ET.Element) -> dict:
    """Parse a perspective element into a dictionary."""
    data = {
        "id": elem.get("id"),
        "name": None,
        "filter_rules": None,
        "value_data": None,
        "date_added": get_text(elem, "added"),
        "date_modified": get_text(elem, "modified"),
    }

    # Parse plist content
    plist_elem = elem.find(f"{NS}plist")
    if plist_elem is not None:
        # Convert plist element to bytes and parse
        plist_str = ET.tostring(plist_elem, encoding="unicode")
        # Remove namespace from plist
        plist_str = plist_str.replace(f' xmlns="{NS[1:-1]}"', "")
        plist_str = plist_str.replace(NS, "")
        try:
            plist_data = plistlib.loads(plist_str.encode("utf-8"))
            data["name"] = plist_data.get("name")
            data["filter_rules"] = plist_data.get("filterRules")
            data["value_data"] = plistlib.dumps(plist_data)
        except Exception:
            pass

    return data


def upsert_task(conn: sqlite3.Connection, data: dict) -> None:
    """Insert or update a task."""
    conn.execute("""
        INSERT INTO Task (id, name, parent_task, project_folder, context,
                         inbox, flagged, date_added, date_modified, date_due,
                         date_start, date_completed, estimated_minutes, rank,
                         note, is_project, project_status, sequential)
        VALUES (:id, :name, :parent_task, :project_folder, :context,
                :inbox, :flagged, :date_added, :date_modified, :date_due,
                :date_start, :date_completed, :estimated_minutes, :rank,
                :note, :is_project, :project_status, :sequential)
        ON CONFLICT(id) DO UPDATE SET
            name = COALESCE(excluded.name, name),
            parent_task = COALESCE(excluded.parent_task, parent_task),
            project_folder = COALESCE(excluded.project_folder, project_folder),
            context = COALESCE(excluded.context, context),
            inbox = COALESCE(excluded.inbox, inbox),
            flagged = COALESCE(excluded.flagged, flagged),
            date_added = COALESCE(excluded.date_added, date_added),
            date_modified = COALESCE(excluded.date_modified, date_modified),
            date_due = COALESCE(excluded.date_due, date_due),
            date_start = COALESCE(excluded.date_start, date_start),
            date_completed = COALESCE(excluded.date_completed, date_completed),
            estimated_minutes = COALESCE(excluded.estimated_minutes, estimated_minutes),
            rank = COALESCE(excluded.rank, rank),
            note = COALESCE(excluded.note, note),
            is_project = COALESCE(excluded.is_project, is_project),
            project_status = COALESCE(excluded.project_status, project_status),
            sequential = COALESCE(excluded.sequential, sequential),
            deleted = 0
    """, {
        "id": data.get("id"),
        "name": data.get("name"),
        "parent_task": data.get("parent_task"),
        "project_folder": data.get("project_folder"),
        "context": data.get("context"),
        "inbox": data.get("inbox", 0),
        "flagged": data.get("flagged", 0),
        "date_added": data.get("date_added"),
        "date_modified": data.get("date_modified"),
        "date_due": data.get("date_due"),
        "date_start": data.get("date_start"),
        "date_completed": data.get("date_completed"),
        "estimated_minutes": data.get("estimated_minutes"),
        "rank": data.get("rank"),
        "note": data.get("note"),
        "is_project": data.get("is_project", 0),
        "project_status": data.get("project_status"),
        "sequential": data.get("sequential", 0),
    })


def upsert_context(conn: sqlite3.Connection, data: dict) -> None:
    """Insert or update a context."""
    conn.execute("""
        INSERT INTO Context (id, name, parent, rank, date_added, date_modified)
        VALUES (:id, :name, :parent, :rank, :date_added, :date_modified)
        ON CONFLICT(id) DO UPDATE SET
            name = COALESCE(excluded.name, name),
            parent = COALESCE(excluded.parent, parent),
            rank = COALESCE(excluded.rank, rank),
            date_added = COALESCE(excluded.date_added, date_added),
            date_modified = COALESCE(excluded.date_modified, date_modified),
            deleted = 0
    """, data)


def upsert_folder(conn: sqlite3.Connection, data: dict) -> None:
    """Insert or update a folder."""
    conn.execute("""
        INSERT INTO Folder (id, name, parent, rank, date_added, date_modified)
        VALUES (:id, :name, :parent, :rank, :date_added, :date_modified)
        ON CONFLICT(id) DO UPDATE SET
            name = COALESCE(excluded.name, name),
            parent = COALESCE(excluded.parent, parent),
            rank = COALESCE(excluded.rank, rank),
            date_added = COALESCE(excluded.date_added, date_added),
            date_modified = COALESCE(excluded.date_modified, date_modified),
            deleted = 0
    """, data)


def upsert_perspective(conn: sqlite3.Connection, data: dict) -> None:
    """Insert or update a perspective."""
    conn.execute("""
        INSERT INTO Perspective (id, name, filter_rules, value_data, date_added, date_modified)
        VALUES (:id, :name, :filter_rules, :value_data, :date_added, :date_modified)
        ON CONFLICT(id) DO UPDATE SET
            name = COALESCE(excluded.name, name),
            filter_rules = COALESCE(excluded.filter_rules, filter_rules),
            value_data = COALESCE(excluded.value_data, value_data),
            date_added = COALESCE(excluded.date_added, date_added),
            date_modified = COALESCE(excluded.date_modified, date_modified)
    """, data)


def get_watermark(conn: sqlite3.Connection) -> str | None:
    """Get the last processed transaction filename."""
    cursor = conn.execute("SELECT value FROM ODOMetadata WHERE key = 'last_transaction'")
    row = cursor.fetchone()
    return row[0] if row else None


def set_watermark(conn: sqlite3.Connection, filename: str) -> None:
    """Set the last processed transaction filename."""
    conn.execute("""
        INSERT INTO ODOMetadata (key, value) VALUES ('last_transaction', :filename)
        ON CONFLICT(key) DO UPDATE SET value = :filename
    """, {"filename": filename})


def discover_transactions(data_dir: Path, after: str | None = None) -> list[Path]:
    """Discover transaction files, optionally filtering to those after watermark."""
    files = sorted(data_dir.glob("*.zip"))

    if after:
        # Filter to files after the watermark
        files = [f for f in files if f.name > after]

    return files


def extract_xml(zip_path: Path) -> ET.Element:
    """Extract and parse contents.xml from a transaction zip."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        with zf.open("contents.xml") as f:
            return ET.parse(f).getroot()


def process_transaction(conn: sqlite3.Connection, zip_path: Path) -> dict:
    """Process a single transaction file. Returns counts of processed entities."""
    counts = {"task": 0, "context": 0, "folder": 0, "perspective": 0}

    root = extract_xml(zip_path)

    for elem in root:
        # Skip reference snapshots
        if elem.get("op") == "reference":
            continue

        # Strip namespace from tag
        tag = elem.tag.replace(NS, "")

        if tag == "task":
            upsert_task(conn, parse_task(elem))
            counts["task"] += 1
        elif tag == "context":
            upsert_context(conn, parse_context(elem))
            counts["context"] += 1
        elif tag == "folder":
            upsert_folder(conn, parse_folder(elem))
            counts["folder"] += 1
        elif tag == "perspective":
            upsert_perspective(conn, parse_perspective(elem))
            counts["perspective"] += 1

    return counts


def main():
    parser = argparse.ArgumentParser(description="Build SQLite from OmniFocus transactions")
    parser.add_argument("--data-dir", "-d", type=Path, default=Path("omnifocus-data"),
                        help="Directory containing transaction .zip files")
    parser.add_argument("--output", "-o", type=Path, default=Path("omnifocus.sqlite"),
                        help="Output SQLite database path")
    parser.add_argument("--full-rebuild", action="store_true",
                        help="Force full rebuild (ignore watermark)")
    args = parser.parse_args()

    # Connect to database
    conn = sqlite3.connect(args.output)
    create_schema(conn)

    # Get watermark (unless full rebuild)
    watermark = None if args.full_rebuild else get_watermark(conn)

    if args.full_rebuild:
        print("Full rebuild - clearing existing data...")
        conn.executescript("""
            DELETE FROM Task;
            DELETE FROM Context;
            DELETE FROM Folder;
            DELETE FROM Perspective;
            DELETE FROM ODOMetadata;
        """)
        conn.commit()

    # Discover transactions
    transactions = discover_transactions(args.data_dir, watermark)

    if not transactions:
        print("No new transactions to process.")
        return

    print(f"Processing {len(transactions)} transaction(s)...")

    total_counts = {"task": 0, "context": 0, "folder": 0, "perspective": 0}

    for i, zip_path in enumerate(transactions):
        print(f"  [{i+1}/{len(transactions)}] {zip_path.name}")
        counts = process_transaction(conn, zip_path)
        for k, v in counts.items():
            total_counts[k] += v
        set_watermark(conn, zip_path.name)
        conn.commit()

    print(f"\nProcessed: {total_counts['task']} tasks, {total_counts['context']} contexts, "
          f"{total_counts['folder']} folders, {total_counts['perspective']} perspectives")

    # Print summary
    cursor = conn.execute("SELECT COUNT(*) FROM Task WHERE deleted = 0")
    task_count = cursor.fetchone()[0]
    cursor = conn.execute("SELECT COUNT(*) FROM Task WHERE deleted = 0 AND date_completed IS NULL")
    open_count = cursor.fetchone()[0]

    print(f"\nDatabase: {args.output}")
    print(f"Total tasks: {task_count} ({open_count} open)")


if __name__ == "__main__":
    main()
