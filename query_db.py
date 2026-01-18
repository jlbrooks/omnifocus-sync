#!/usr/bin/env python3
"""
Query OmniFocus SQLite database.

Usage:
    python query_db.py tasks              # List open tasks
    python query_db.py tasks --all        # Include completed
    python query_db.py tasks --inbox      # Inbox only
    python query_db.py tasks --flagged    # Flagged only
    python query_db.py tasks --due        # With due dates
    python query_db.py projects           # List projects
    python query_db.py contexts           # List contexts/tags
    python query_db.py folders            # List folders
    python query_db.py perspectives       # List perspectives
    python query_db.py sql "SELECT ..."   # Raw SQL query
"""

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


def format_date(iso_date: str | None) -> str:
    """Format ISO date for display."""
    if not iso_date:
        return ""
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return iso_date[:10] if iso_date else ""


def parse_filter_rule(rule: dict, negated: bool = False) -> list[str]:
    """Parse a single filter rule into SQL conditions.

    Args:
        rule: The filter rule dict
        negated: If True, generate NULL-safe negated conditions
    """
    conditions = []

    # Direct rules
    if "actionAvailability" in rule:
        avail = rule["actionAvailability"]
        if avail == "available":
            # Available = not completed AND (no defer date OR defer date <= today)
            if negated:
                # NOT available = completed OR deferred to future
                conditions.append("(t.date_completed IS NOT NULL OR (t.date_start IS NOT NULL AND date(t.date_start) > date('now')))")
            else:
                conditions.append("t.date_completed IS NULL")
                conditions.append("(t.date_start IS NULL OR date(t.date_start) <= date('now'))")
        elif avail == "remaining":
            # Remaining = not completed (may still be deferred)
            if negated:
                conditions.append("t.date_completed IS NOT NULL")
            else:
                conditions.append("t.date_completed IS NULL")
        elif avail == "completed":
            if negated:
                conditions.append("t.date_completed IS NULL")
            else:
                conditions.append("t.date_completed IS NOT NULL")

    if "actionHasDueDate" in rule and rule["actionHasDueDate"]:
        if negated:
            conditions.append("t.date_due IS NULL")
        else:
            conditions.append("t.date_due IS NOT NULL")

    if "actionHasDeferDate" in rule and rule["actionHasDeferDate"]:
        if negated:
            conditions.append("t.date_start IS NULL")
        else:
            conditions.append("t.date_start IS NOT NULL")

    if "actionIsLeaf" in rule and rule["actionIsLeaf"]:
        if negated:
            conditions.append("t.is_project = 1")
        else:
            conditions.append("t.is_project = 0")

    if "actionWithinFocus" in rule:
        ids = rule["actionWithinFocus"]
        id_list = ",".join(f"'{id}'" for id in ids)
        if negated:
            # NULL-safe negation: NULL means not in the focus, so include those rows
            # (col IS NULL OR col NOT IN (...)) for each column
            conditions.append(
                f"((t.project_folder IS NULL OR t.project_folder NOT IN ({id_list})) "
                f"AND (t.parent_task IS NULL OR t.parent_task NOT IN ({id_list})) "
                f"AND (p.project_folder IS NULL OR p.project_folder NOT IN ({id_list})))"
            )
        else:
            # Check task's folder, parent task (project), or parent project's folder
            conditions.append(f"(t.project_folder IN ({id_list}) OR t.parent_task IN ({id_list}) OR p.project_folder IN ({id_list}))")

    if "actionHasAnyOfTags" in rule:
        tag_ids = rule["actionHasAnyOfTags"]
        tag_list = ",".join(f"'{id}'" for id in tag_ids)
        if negated:
            conditions.append(f"(t.context IS NULL OR t.context NOT IN ({tag_list}))")
        else:
            conditions.append(f"t.context IN ({tag_list})")

    return conditions


def get_perspective_conditions(conn: sqlite3.Connection, perspective_name: str) -> list[str]:
    """Convert perspective filter rules to SQL conditions."""
    cursor = conn.execute(
        "SELECT filter_rules FROM Perspective WHERE name = ? OR id = ?",
        (perspective_name, perspective_name)
    )
    row = cursor.fetchone()
    if not row or not row[0]:
        return []

    rules = json.loads(row[0])
    conditions = []

    for rule in rules:
        # Handle direct rule properties
        direct_conditions = parse_filter_rule(rule)
        conditions.extend(direct_conditions)

        # Handle aggregate rules
        if "aggregateType" in rule:
            agg_type = rule["aggregateType"]
            agg_rules = rule.get("aggregateRules", [])

            sub_condition_groups = []
            for sub_rule in agg_rules:
                # Skip disabled rules
                if "disabledRule" in sub_rule:
                    continue
                sub_conds = parse_filter_rule(sub_rule)
                if sub_conds:
                    sub_condition_groups.append(sub_conds)

            if sub_condition_groups:
                if agg_type == "any":
                    # Each sub-rule's conditions are ANDed, then OR across sub-rules
                    or_parts = []
                    for group in sub_condition_groups:
                        if len(group) == 1:
                            or_parts.append(group[0])
                        elif len(group) > 1:
                            or_parts.append(f"({' AND '.join(group)})")
                    if or_parts:
                        conditions.append(f"({' OR '.join(or_parts)})")
                elif agg_type == "all":
                    for group in sub_condition_groups:
                        conditions.extend(group)
                elif agg_type == "none":
                    # "none" = match NONE of the rules = NOT(any) = NOT r1 AND NOT r2 AND ...
                    # Negate ALL rules and AND them together
                    for sub_rule in agg_rules:
                        if "disabledRule" in sub_rule:
                            continue
                        negated_conds = parse_filter_rule(sub_rule, negated=True)
                        conditions.extend(negated_conds)

    return conditions


def list_tasks(conn: sqlite3.Connection, args) -> None:
    """List tasks with optional filters."""
    conditions = ["t.deleted = 0"]

    # Apply perspective filters if specified
    if hasattr(args, 'perspective') and args.perspective:
        perspective_conditions = get_perspective_conditions(conn, args.perspective)
        conditions.extend(perspective_conditions)
    else:
        if not args.all:
            conditions.append("t.date_completed IS NULL")

    if args.inbox:
        conditions.append("t.inbox = 1")

    if args.flagged:
        conditions.append("t.flagged = 1")

    if args.due:
        conditions.append("t.date_due IS NOT NULL")

    if args.context:
        conditions.append(f"t.context = (SELECT id FROM Context WHERE name = '{args.context}')")

    if args.project:
        conditions.append(f"t.parent_task = (SELECT id FROM Task WHERE name = '{args.project}' AND is_project = 1)")

    where = " AND ".join(conditions)

    query = f"""
        SELECT t.id, t.name, t.date_due, t.date_start, t.flagged, c.name as context_name,
               p.name as project_name
        FROM Task t
        LEFT JOIN Context c ON t.context = c.id
        LEFT JOIN Task p ON t.parent_task = p.id
        WHERE {where}
        ORDER BY t.flagged DESC, t.date_due ASC NULLS LAST, t.rank
        LIMIT {args.limit}
    """

    cursor = conn.execute(query)
    rows = cursor.fetchall()

    if args.json:
        print(json.dumps([{
            "id": r[0],
            "name": r[1],
            "due": r[2],
            "defer": r[3],
            "flagged": bool(r[4]),
            "context": r[5],
            "project": r[6]
        } for r in rows], indent=2))
        return

    print(f"{'Name':<40} {'Due':<12} {'Defer':<12} {'Project':<25}")
    print("-" * 89)

    for row in rows:
        name = (row[1] or "(no name)")[:38]
        flag = "⚑ " if row[4] else "  "
        due = format_date(row[2])
        defer = format_date(row[3])
        project = (row[6] or "")[:23]
        print(f"{flag}{name:<38} {due:<12} {defer:<12} {project:<25}")

    print(f"\n{len(rows)} task(s)")


def list_projects(conn: sqlite3.Connection, args) -> None:
    """List projects."""
    conditions = ["t.is_project = 1", "t.deleted = 0"]

    if not args.all:
        conditions.append("t.date_completed IS NULL")
        conditions.append("(t.project_status IS NULL OR t.project_status = 'active')")

    where = " AND ".join(conditions)

    query = f"""
        SELECT t.id, t.name, t.project_status, f.name as folder_name,
               (SELECT COUNT(*) FROM Task sub WHERE sub.parent_task = t.id AND sub.date_completed IS NULL AND sub.deleted = 0) as open_tasks
        FROM Task t
        LEFT JOIN Folder f ON t.project_folder = f.id
        WHERE {where}
        ORDER BY f.name, t.rank
        LIMIT {args.limit}
    """

    cursor = conn.execute(query)
    rows = cursor.fetchall()

    if args.json:
        print(json.dumps([{
            "id": r[0],
            "name": r[1],
            "status": r[2],
            "folder": r[3],
            "open_tasks": r[4]
        } for r in rows], indent=2))
        return

    print(f"{'Project':<40} {'Status':<10} {'Folder':<20} {'Open':<6}")
    print("-" * 76)

    for row in rows:
        name = (row[1] or "(no name)")[:38]
        status = (row[2] or "active")[:8]
        folder = (row[3] or "")[:18]
        open_tasks = row[4]
        print(f"{name:<40} {status:<10} {folder:<20} {open_tasks:<6}")

    print(f"\n{len(rows)} project(s)")


def list_contexts(conn: sqlite3.Connection, args) -> None:
    """List contexts/tags."""
    query = """
        SELECT c.id, c.name, p.name as parent_name,
               (SELECT COUNT(*) FROM Task t WHERE t.context = c.id AND t.date_completed IS NULL AND t.deleted = 0) as task_count
        FROM Context c
        LEFT JOIN Context p ON c.parent = p.id
        WHERE c.deleted = 0
        ORDER BY c.rank
    """

    cursor = conn.execute(query)
    rows = cursor.fetchall()

    if args.json:
        print(json.dumps([{
            "id": r[0],
            "name": r[1],
            "parent": r[2],
            "open_tasks": r[3]
        } for r in rows], indent=2))
        return

    print(f"{'Context':<30} {'Parent':<20} {'Open Tasks':<10}")
    print("-" * 60)

    for row in rows:
        name = (row[1] or "(no name)")[:28]
        parent = (row[2] or "")[:18]
        task_count = row[3]
        print(f"{name:<30} {parent:<20} {task_count:<10}")

    print(f"\n{len(rows)} context(s)")


def list_folders(conn: sqlite3.Connection, args) -> None:
    """List folders."""
    query = """
        SELECT f.id, f.name, p.name as parent_name,
               (SELECT COUNT(*) FROM Task t WHERE t.project_folder = f.id AND t.is_project = 1 AND t.deleted = 0) as project_count
        FROM Folder f
        LEFT JOIN Folder p ON f.parent = p.id
        WHERE f.deleted = 0
        ORDER BY f.rank
    """

    cursor = conn.execute(query)
    rows = cursor.fetchall()

    if args.json:
        print(json.dumps([{
            "id": r[0],
            "name": r[1],
            "parent": r[2],
            "projects": r[3]
        } for r in rows], indent=2))
        return

    print(f"{'Folder':<35} {'Parent':<25} {'Projects':<10}")
    print("-" * 70)

    for row in rows:
        name = (row[1] or "(no name)")[:33]
        parent = (row[2] or "")[:23]
        project_count = row[3]
        print(f"{name:<35} {parent:<25} {project_count:<10}")

    print(f"\n{len(rows)} folder(s)")


def list_perspectives(conn: sqlite3.Connection, args) -> None:
    """List perspectives."""
    query = "SELECT id, name, filter_rules FROM Perspective ORDER BY name"

    cursor = conn.execute(query)
    rows = cursor.fetchall()

    if args.json:
        print(json.dumps([{
            "id": r[0],
            "name": r[1],
            "filter_rules": r[2]
        } for r in rows], indent=2))
        return

    print(f"{'ID':<25} {'Name':<30}")
    print("-" * 55)

    for row in rows:
        id_ = (row[0] or "")[:23]
        name = (row[1] or "(no name)")[:28]
        print(f"{id_:<25} {name:<30}")

    print(f"\n{len(rows)} perspective(s)")


def run_sql(conn: sqlite3.Connection, args) -> None:
    """Run raw SQL query."""
    cursor = conn.execute(args.query)
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description] if cursor.description else []

    if args.json:
        print(json.dumps([dict(zip(columns, row)) for row in rows], indent=2, default=str))
        return

    if columns:
        print("\t".join(columns))
        print("-" * (len(columns) * 20))

    for row in rows:
        print("\t".join(str(v) if v is not None else "" for v in row))

    print(f"\n{len(rows)} row(s)")


def main():
    parser = argparse.ArgumentParser(description="Query OmniFocus database")
    parser.add_argument("--db", "-d", type=Path, default=Path("omnifocus.sqlite"),
                        help="Database path")
    parser.add_argument("--json", "-j", action="store_true",
                        help="Output as JSON")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Tasks
    tasks_parser = subparsers.add_parser("tasks", help="List tasks")
    tasks_parser.add_argument("--perspective", "--persp", help="Filter by perspective name (e.g., Available, Work)")
    tasks_parser.add_argument("--all", "-a", action="store_true", help="Include completed")
    tasks_parser.add_argument("--inbox", "-i", action="store_true", help="Inbox only")
    tasks_parser.add_argument("--flagged", "-f", action="store_true", help="Flagged only")
    tasks_parser.add_argument("--due", action="store_true", help="With due dates only")
    tasks_parser.add_argument("--context", "-c", help="Filter by context name")
    tasks_parser.add_argument("--project", "-p", help="Filter by project name")
    tasks_parser.add_argument("--limit", "-n", type=int, default=100, help="Max results")

    # Projects
    projects_parser = subparsers.add_parser("projects", help="List projects")
    projects_parser.add_argument("--all", "-a", action="store_true", help="Include completed/dropped")
    projects_parser.add_argument("--limit", "-n", type=int, default=100, help="Max results")

    # Contexts
    subparsers.add_parser("contexts", help="List contexts/tags")

    # Folders
    subparsers.add_parser("folders", help="List folders")

    # Perspectives
    subparsers.add_parser("perspectives", help="List perspectives")

    # SQL
    sql_parser = subparsers.add_parser("sql", help="Run raw SQL")
    sql_parser.add_argument("query", help="SQL query")

    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    if args.command == "tasks":
        list_tasks(conn, args)
    elif args.command == "projects":
        list_projects(conn, args)
    elif args.command == "contexts":
        list_contexts(conn, args)
    elif args.command == "folders":
        list_folders(conn, args)
    elif args.command == "perspectives":
        list_perspectives(conn, args)
    elif args.command == "sql":
        run_sql(conn, args)


if __name__ == "__main__":
    main()
