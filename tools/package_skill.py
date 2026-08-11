#!/usr/bin/env python3
"""Package the ats-check skill into a zip for upload to claude.ai.

    python3 tools/package_skill.py

Produces ``dist/ats-check.zip`` laid out as claude.ai expects: a single
top-level folder whose name matches the skill, with SKILL.md at its root.

Validates before zipping, because a skill that uploads and then fails to
trigger is worse than one that refuses to build.
"""

from __future__ import annotations

import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT, ".claude", "skills", "ats-check")
DIST = os.path.join(ROOT, "dist")
SKILL_NAME = "ats-check"

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".git", ".ipynb_checkpoints"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".DS_Store", ".swp")

# claude.ai limits; keep headroom rather than shipping right at the edge.
MAX_DESCRIPTION = 1024
MAX_NAME = 64


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def parse_frontmatter(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        fail("SKILL.md has no YAML frontmatter block")
    meta: dict = {}
    key = None
    for line in m.group(1).split("\n"):
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if km:
            key = km.group(1)
            meta[key] = km.group(2).strip()
        elif key and line.strip():
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta


def validate() -> dict:
    if not os.path.isdir(SKILL_DIR):
        fail(f"skill directory not found: {SKILL_DIR}")
    skill_md = os.path.join(SKILL_DIR, "SKILL.md")
    if not os.path.isfile(skill_md):
        fail("SKILL.md is missing")

    meta = parse_frontmatter(skill_md)
    name = meta.get("name", "")
    desc = meta.get("description", "")

    if not name:
        fail("frontmatter is missing 'name'")
    if name != SKILL_NAME:
        fail(f"frontmatter name '{name}' does not match directory '{SKILL_NAME}'")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        fail(f"name '{name}' must be lowercase letters, digits and hyphens")
    if len(name) > MAX_NAME:
        fail(f"name is {len(name)} chars, limit {MAX_NAME}")
    if not desc:
        fail("frontmatter is missing 'description' -- the skill would never trigger")
    if len(desc) > MAX_DESCRIPTION:
        fail(f"description is {len(desc)} chars, limit {MAX_DESCRIPTION}")

    # Every script must at least compile.
    import py_compile

    scripts = os.path.join(SKILL_DIR, "scripts")
    if os.path.isdir(scripts):
        for fn in sorted(os.listdir(scripts)):
            if fn.endswith(".py"):
                try:
                    py_compile.compile(os.path.join(scripts, fn), doraise=True)
                except py_compile.PyCompileError as exc:
                    fail(f"{fn} does not compile: {exc}")

    # Reject third-party imports: the sandbox has no network to install them.
    stdlib_ok = True
    third_party = re.compile(
        r"^\s*(?:import|from)\s+(numpy|pandas|pypdf|PyPDF2|fitz|pdfplumber|docx|requests|"
        r"bs4|lxml|sklearn|scipy|matplotlib|google|openai|anthropic)\b", re.M)
    for dirpath, _dirs, files in os.walk(scripts):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(dirpath, fn), "r", encoding="utf-8") as fh:
                hit = third_party.search(fh.read())
            if hit:
                print(f"error: {fn} imports third-party module '{hit.group(1)}'", file=sys.stderr)
                stdlib_ok = False
    if not stdlib_ok:
        fail("skill must depend on the standard library only")

    return meta


def build(meta: dict) -> str:
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, f"{SKILL_NAME}.zip")
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirs, files in os.walk(SKILL_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for fn in sorted(files):
                if fn.endswith(EXCLUDE_SUFFIX):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, SKILL_DIR)
                z.write(full, os.path.join(SKILL_NAME, rel))
                count += 1
    size = os.path.getsize(out)
    print(f"built {os.path.relpath(out, ROOT)}  ({count} files, {size / 1024:.0f} KB)")
    print(f"  name:        {meta['name']}")
    print(f"  description: {meta['description'][:100]}...")
    return out


def main() -> int:
    meta = validate()
    build(meta)
    print("\nUpload it at claude.ai > Settings > Capabilities > Skills > Upload skill.")
    print("Code execution must be enabled for the skill's scripts to run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
