#!/usr/bin/env python3
"""
generate_workforce_md.py

Reads FOLDER_SUPERSET from static/js/folder-superset.js and generates:
  - workforce/<id>.md for each agent (72 files)
  - workforce/workforce-map.md with the full hierarchy

Usage:
    python scripts/generate_workforce_md.py
"""

import os
import re
import sys
from pathlib import Path


def find_project_root():
    """Walk up from this script to find the project root (where static/ lives)."""
    here = Path(__file__).resolve().parent
    for ancestor in [here, *here.parents]:
        if (ancestor / "static" / "js" / "folder-superset.js").exists():
            return ancestor
    print("ERROR: Could not find static/js/folder-superset.js from script location.")
    sys.exit(1)


def read_js_file(path):
    """Read the JS source file."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def extract_entries(js_source):
    """
    Parse FOLDER_SUPERSET entries from the JS source using regex.

    Returns a dict keyed by entry id, each value is a dict with:
        name, parentId, children, label, systemPrompt
    """
    entries = {}

    # Match each top-level entry:  'key-name': { ... },\n  },(end) or next entry
    # We use a two-pass approach: first find all entry keys and their positions,
    # then extract fields from each entry block.

    # Find the FOLDER_SUPERSET object boundaries
    superset_start = js_source.find("const FOLDER_SUPERSET = {")
    if superset_start == -1:
        print("ERROR: Could not find FOLDER_SUPERSET in JS source.")
        sys.exit(1)

    # Find the closing }; for FOLDER_SUPERSET (before FOLDER_TEMPLATES)
    templates_start = js_source.find("const FOLDER_TEMPLATES = {")
    if templates_start == -1:
        superset_body = js_source[superset_start:]
    else:
        superset_body = js_source[superset_start:templates_start]

    # Find all entry start positions: 'key-name': {
    entry_pattern = re.compile(r"'([a-z][a-z0-9-]*)'\s*:\s*\{")
    entry_starts = list(entry_pattern.finditer(superset_body))

    for i, match in enumerate(entry_starts):
        entry_id = match.group(1)
        start = match.start()

        # Determine the end of this entry block: the start of the next entry,
        # or end of superset_body
        if i + 1 < len(entry_starts):
            end = entry_starts[i + 1].start()
        else:
            end = len(superset_body)

        block = superset_body[start:end]

        # Extract name
        name_match = re.search(r"name:\s*'([^']*)'", block)
        name = name_match.group(1) if name_match else entry_id

        # Extract parentId
        parent_match = re.search(r"parentId:\s*(null|'([^']*)')", block)
        if parent_match:
            parent_id = parent_match.group(2)  # None if null
        else:
            parent_id = None

        # Extract children list
        children_match = re.search(r"children:\s*\[([^\]]*)\]", block)
        children = []
        if children_match:
            children_str = children_match.group(1).strip()
            if children_str:
                children = re.findall(r"'([^']*)'", children_str)

        # Extract skill.label
        label_match = re.search(r"label:\s*'([^']*)'", block)
        label = label_match.group(1) if label_match else name

        # Extract skill.systemPrompt — these are long single-quoted strings
        # that may contain escaped single quotes (\')
        # We need to handle: systemPrompt: '...',  where the string may contain \'
        prompt_match = re.search(r"systemPrompt:\s*'((?:[^'\\]|\\.)*)'", block)
        if prompt_match:
            system_prompt = prompt_match.group(1)
            # Unescape \' back to '
            system_prompt = system_prompt.replace("\\'", "'")
            # Unescape other common JS escapes
            system_prompt = system_prompt.replace("\\n", "\n")
            system_prompt = system_prompt.replace("\\\\", "\\")
        else:
            system_prompt = ""

        # Handle Unicode escapes like \u2014
        system_prompt = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda m: chr(int(m.group(1), 16)),
            system_prompt,
        )

        entries[entry_id] = {
            "name": name,
            "parentId": parent_id,
            "children": children,
            "label": label,
            "systemPrompt": system_prompt,
        }

    return entries


def resolve_department(entry_id, entries):
    """
    Determine the department name for an entry.
    - Top-level departments (parentId is None) use their own name.
    - Children use the parent's name.
    """
    entry = entries[entry_id]
    if entry["parentId"] is None:
        return entry["name"]
    parent_id = entry["parentId"]
    if parent_id in entries:
        return entries[parent_id]["name"]
    # Fallback
    return entry["name"]


def write_agent_md(output_dir, entry_id, entry, department):
    """Write a single agent .md file."""
    filepath = output_dir / f"{entry_id}.md"
    content = (
        f"---\n"
        f"id: {entry_id}\n"
        f"name: {entry['label']}\n"
        f"department: {department}\n"
        f"---\n"
        f"\n"
        f"{entry['systemPrompt']}\n"
    )
    filepath.write_text(content, encoding="utf-8")
    return filepath


def build_hierarchy(entries):
    """
    Build the hierarchy tree.
    Returns a list of (entry_id, children_ids) for top-level departments,
    in the order they appear in the entries dict.
    """
    top_level = []
    for entry_id, entry in entries.items():
        if entry["parentId"] is None:
            top_level.append(entry_id)
    return top_level


def write_workforce_map(output_dir, entries):
    """Write the workforce-map.md file."""
    filepath = output_dir / "workforce-map.md"
    lines = [
        "---",
        "type: workforce-map",
        "name: Default Workforce",
        "version: 1",
        "---",
        "",
    ]

    top_level = build_hierarchy(entries)
    for dept_id in top_level:
        dept = entries[dept_id]
        lines.append(f"- {dept_id}: {dept['label']}")
        for child_id in dept["children"]:
            if child_id in entries:
                child = entries[child_id]
                lines.append(f"  - {child_id}: {child['label']}")

    filepath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return filepath


def main():
    project_root = find_project_root()
    js_path = project_root / "static" / "js" / "folder-superset.js"

    print(f"Reading {js_path}")
    js_source = read_js_file(js_path)

    print("Parsing FOLDER_SUPERSET entries...")
    entries = extract_entries(js_source)
    print(f"Found {len(entries)} entries.")

    if len(entries) == 0:
        print("ERROR: No entries found. Check the JS file format.")
        sys.exit(1)

    # Create output directory
    output_dir = project_root / "workforce"
    output_dir.mkdir(exist_ok=True)
    print(f"Output directory: {output_dir}")

    # Write individual agent .md files
    count = 0
    for entry_id, entry in entries.items():
        department = resolve_department(entry_id, entries)
        write_agent_md(output_dir, entry_id, entry, department)
        count += 1

    print(f"Wrote {count} agent .md files.")

    # Write workforce-map.md
    map_path = write_workforce_map(output_dir, entries)
    print(f"Wrote {map_path}")

    # Summary
    top_level = [eid for eid, e in entries.items() if e["parentId"] is None]
    children = [eid for eid, e in entries.items() if e["parentId"] is not None]
    print(f"\nSummary:")
    print(f"  Top-level departments: {len(top_level)}")
    print(f"  Child agents:          {len(children)}")
    print(f"  Total agents:          {count}")
    print(f"  Workforce map:         workforce/workforce-map.md")
    print("Done.")


if __name__ == "__main__":
    main()
