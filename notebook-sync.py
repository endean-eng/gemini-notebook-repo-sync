#!/usr/bin/env python3
"""
notebook-sync.py
Zero-dependency GitHub-to-Google Drive Native Doc Bundler for Gemini Notebooks.

Features:
- Uses native Git blob hashes (`git ls-files -s`) for deterministic file tracking.
- Enforces strict Root isolation: Root-level files ALWAYS occupy their own `00-ROOT`
  shard and never merge with subdirectories, even if the whole repo is <450k chars.
- Prunes empty intermediate folders (folders containing only a single subfolder
  never generate empty Google Docs).
- Compacts deep leaf directories bottom-up up to MAX_SHARD_CHARS (450,000 chars),
  stopping before Depth 1 (top-level directories).
- Splits oversized single directories into `-part1`, `-part2`, etc.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple

# ============================================================================
# CONFIGURATION & THRESHOLDS
# ============================================================================
MAX_SHARD_CHARS = int(os.environ.get("MAX_SHARD_CHARS", "450000"))
MAX_SINGLE_FILE_BYTES = int(os.environ.get("MAX_SINGLE_FILE_BYTES", "350000"))

# Files and extensions excluded from Notebook RAG bundles (binaries, lockfiles, media)
EXCLUDED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf",
    ".zip", ".tar", ".gz", ".7z", ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".mp4", ".mov", ".avi", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".class", ".o", ".obj", ".lock", ".sqlite", ".db"
}

EXCLUDED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "Cargo.lock", "poetry.lock", "Pipfile.lock", "composer.lock",
    ".DS_Store", "Thumbs.db"
}

EXCLUDED_PATH_PREFIXES = (
    ".git/",
    "node_modules/",
    "dist/",
    "build/",
    "out/",
    ".next/",
    "coverage/",
    "__pycache__/",
    ".venv/",
    "venv/",
)

EXTENSION_TO_LANG = {
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx",
    ".py": "python", ".sh": "bash", ".bash": "bash", ".zsh": "zsh",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".md": "markdown", ".html": "html", ".css": "css", ".scss": "scss",
    ".sql": "sql", ".rs": "rust", ".go": "go", ".java": "java",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".cs": "csharp", ".rb": "ruby",
    ".php": "php", ".xml": "xml", ".dockerfile": "dockerfile"
}


# ============================================================================
# STEP 1: GIT INDEX & BLOB HASH EXTRACTION
# ============================================================================
def get_git_tracked_files() -> List[Tuple[str, str]]:
    """
    Returns a list of (rel_path, git_blob_sha1) for all tracked files in the repo
    using `git ls-files -s`.
    """
    proc = subprocess.run(
        ["git", "ls-files", "-s"],
        capture_output=True,
        text=True,
        check=True
    )
    entries: List[Tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        # Format: <mode> <blob_sha1> <stage>\t<path>
        meta, rel_path = line.split("\t", 1)
        parts = meta.split()
        mode, blob_sha = parts[0], parts[1]
        # Skip gitlinks / submodules (mode 160000)
        if mode == "160000":
            continue
        entries.append((rel_path.replace("\\", "/"), blob_sha))
    return sorted(entries, key=lambda x: x[0])


def get_head_commit_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        )
        return proc.stdout.strip()
    except Exception:
        return "unknown"


def should_include_file(rel_path: str) -> bool:
    if any(rel_path.startswith(prefix) for prefix in EXCLUDED_PATH_PREFIXES):
        return False
    path_obj = Path(rel_path)
    if path_obj.name in EXCLUDED_FILENAMES:
        return False
    if path_obj.suffix.lower() in EXCLUDED_EXTENSIONS:
        return False
    if not path_obj.is_file():
        return False
    try:
        size = path_obj.stat().st_size
        if size == 0 or size > MAX_SINGLE_FILE_BYTES:
            return False
        # Reject binary files containing null bytes in the first 4KB
        with open(path_obj, "rb") as f:
            chunk = f.read(4096)
            if b"\x00" in chunk:
                return False
    except OSError:
        return False
    return True


def format_file_block(rel_path: str, blob_sha: str) -> str:
    """
    Formats a single file with an explicit path/hash header and fenced code block
    so Gemini Notebook RAG chunks retain file provenance.
    """
    path_obj = Path(rel_path)
    raw_text = path_obj.read_text(encoding="utf-8", errors="replace")
    line_count = raw_text.count("\n") + (1 if raw_text and not raw_text.endswith("\n") else 0)
    ext = path_obj.suffix.lower()
    lang = EXTENSION_TO_LANG.get(ext, "")

    # Escape triple backticks inside the file if present by using a 4-backtick outer fence
    fence = "````" if "```" in raw_text else "```"
    body = raw_text if raw_text.endswith("\n") else raw_text + "\n"

    return (
        f"\n# ============================================================================\n"
        f"# FILE: {rel_path}\n"
        f"# GIT_BLOB_SHA1: {blob_sha} | LINES: {line_count} | CHARS: {len(raw_text)}\n"
        f"# ============================================================================\n"
        f"{fence}{lang}\n"
        f"{body}"
        f"{fence}\n"
    )


# ============================================================================
# STEP 2: BOTTOM-UP DIRECTORY COMPACTION (WITH STRICT ROOT ISOLATION)
# ============================================================================
class DirNode:
    def __init__(self, path: str):
        self.path = path  # "" for root, "src/core" for subdirs
        self.children: Dict[str, "DirNode"] = {}
        # List of tuples: (rel_path, blob_sha, formatted_block)
        self.files: List[Tuple[str, str, str]] = []

    @property
    def depth(self) -> int:
        if not self.path:
            return 0
        return len(self.path.split("/"))

    @property
    def direct_chars(self) -> int:
        return sum(len(item[2]) for item in self.files)


def build_And_compact_shards(
    tracked_files: List[Tuple[str, str]]
) -> Dict[str, List[Tuple[str, str, str]]]:
    """
    1. Groups files by exact parent directory.
    2. Isolates root files (`path == ""`) into `00-ROOT` unconditionally.
    3. Walks non-root directories bottom-up (deepest first):
       - Empty pass-through directories (0 files) produce no shard.
       - Leaf directories merge upward into their parent directory if their
         combined size stays <= MAX_SHARD_CHARS, stopping at Depth 1 (top-level
         subdirectories like `src/`, `docs/`, `tests/`) so root is never merged.
    """
    root = DirNode("")
    nodes_by_path: Dict[str, DirNode] = {"": root}

    def get_or_create_node(dir_path: str) -> DirNode:
        if dir_path in nodes_by_path:
            return nodes_by_path[dir_path]
        parts = dir_path.split("/")
        cur_path = ""
        parent_node = root
        for part in parts:
            cur_path = f"{cur_path}/{part}" if cur_path else part
            if cur_path not in nodes_by_path:
                new_node = DirNode(cur_path)
                nodes_by_path[cur_path] = new_node
                parent_node.children[part] = new_node
            parent_node = nodes_by_path[cur_path]
        return parent_node

    # Populate tree with eligible files
    for rel_path, blob_sha in tracked_files:
        if not should_include_file(rel_path):
            continue
        parent_dir = os.path.dirname(rel_path).replace("\\", "/")
        node = get_or_create_node(parent_dir)
        node.files.append((rel_path, blob_sha, format_file_block(rel_path, blob_sha)))

    finalized_shards: Dict[str, List[Tuple[str, str, str]]] = {}

    # Rule A: Root files ALWAYS get their own dedicated shard(s), never merged with subdirs
    if root.files:
        finalized_shards["00-ROOT"] = sorted(root.files, key=lambda x: x[0])

    # Rule B: Bottom-up compaction of all subdirectories (depth >= 1)
    non_root_nodes = [n for p, n in nodes_by_path.items() if p != ""]
    # Sort deepest directories first
    non_root_nodes.sort(key=lambda n: (-n.depth, n.path))

    for node in non_root_nodes:
        # If this directory is a depth > 1 subfolder, check if it can merge into its parent
        if node.depth > 1:
            parent_path = os.path.dirname(node.path).replace("\\", "/")
            parent_node = nodes_by_path[parent_path]

            # Empty directory with 0 accumulated files -> pruned automatically
            if not node.files:
                continue

            # If merging into parent keeps parent <= MAX_SHARD_CHARS, bubble files up
            if (parent_node.direct_chars + node.direct_chars) <= MAX_SHARD_CHARS:
                parent_node.files.extend(node.files)
                node.files = []
            else:
                # Freeze this subfolder as its own standalone shard
                shard_key = f"01-{node.path.replace('/', '-')}"
                finalized_shards[shard_key] = sorted(node.files, key=lambda x: x[0])
                node.files = []
        else:
            # Depth == 1 (Top-level folder like `src`, `docs`, `scripts`)
            # Never merges into root! If it has files (direct or bubbled up), freeze it.
            if node.files:
                shard_key = f"01-{node.path.replace('/', '-')}"
                finalized_shards[shard_key] = sorted(node.files, key=lambda x: x[0])
                node.files = []

    # Rule C: Split any shard that exceeds MAX_SHARD_CHARS (e.g. a single massive flat folder)
    size_bounded_shards: Dict[str, List[Tuple[str, str, str]]] = {}
    for shard_key in sorted(finalized_shards.keys()):
        items = finalized_shards[shard_key]
        total_chars = sum(len(x[2]) for x in items)
        if total_chars <= MAX_SHARD_CHARS or len(items) <= 1:
            size_bounded_shards[shard_key] = items
        else:
            part_idx = 1
            current_bucket: List[Tuple[str, str, str]] = []
            current_chars = 0
            for item in items:
                item_len = len(item[2])
                if current_bucket and (current_chars + item_len) > MAX_SHARD_CHARS:
                    size_bounded_shards[f"{shard_key}-part{part_idx}"] = current_bucket
                    part_idx += 1
                    current_bucket = [item]
                    current_chars = item_len
                else:
                    current_bucket.append(item)
                    current_chars += item_len
            if current_bucket:
                size_bounded_shards[f"{shard_key}-part{part_idx}"] = current_bucket

    return size_bounded_shards


# ============================================================================
# STEP 3: ASSEMBLE PAYLOAD & PUSH TO GOOGLE APPS SCRIPT WEB APP
# ============================================================================
def build_shard_document(
    repo_name: str,
    shard_name: str,
    head_sha: str,
    items: List[Tuple[str, str, str]]
) -> Tuple[str, str]:
    """
    Returns (document_text, composite_shard_sha256).
    The composite hash is computed strictly from (rel_path + git_blob_sha1) of the
    member files, so unchanged shards produce an identical hash across runs.
    """
    hasher = hashlib.sha256()
    manifest_lines = []
    for rel_path, blob_sha, _ in items:
        hasher.update(f"{rel_path}:{blob_sha}\n".encode("utf-8"))
        manifest_lines.append(f"- `{rel_path}` (git-blob: `{blob_sha[:10]}`)")

    shard_hash = hasher.hexdigest()[:16]

    header = (
        f"# REPOSITORY: {repo_name}\n"
        f"# SHARD: {shard_name}\n"
        f"# GIT_HEAD_COMMIT: {head_sha}\n"
        f"# SHARD_COMPOSITE_HASH: {shard_hash}\n"
        f"# TOTAL_FILES: {len(items)}\n\n"
        f"## SHARD FILE MANIFEST\n"
        + "\n".join(manifest_lines)
        + "\n\n"
        + ("=" * 76)
        + "\n"
    )

    full_text = header + "".join(block for _, _, block in items)
    return full_text, shard_hash


def post_shard_to_gas(
    webhook_url: str,
    secret: str,
    repo_name: str,
    shard_name: str,
    content: str,
    shard_hash: str,
    active_shards: List[str] | None
) -> dict:
    payload = {
        "secret": secret,
        "repo": repo_name,
        "shardName": shard_name,
        "shardHash": shard_hash,
        "content": content,
    }
    if active_shards is not None:
        payload["activeShards"] = active_shards

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    # Note: Google Apps Script Web Apps respond with an HTTP 302 redirect to
    # script.googleusercontent.com; Python's urllib follows this automatically.
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw_resp = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw_resp)


def main() -> int:
    webhook_url = os.environ.get("GAS_WEBHOOK_URL", "").strip()
    webhook_secret = os.environ.get("GAS_WEBHOOK_SECRET", "").strip()

    # In GitHub Actions, GITHUB_REPOSITORY is "owner/repo-name". Extract just "repo-name".
    raw_repo = os.environ.get("REPO_NAME") or os.environ.get("GITHUB_REPOSITORY", "")
    repo_name = raw_repo.split("/")[-1].strip() if raw_repo else Path.cwd().name

    dry_run = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

    if not dry_run and (not webhook_url or not webhook_secret):
        print("ERROR: GAS_WEBHOOK_URL and GAS_WEBHOOK_SECRET environment variables are required.", file=sys.stderr)
        return 1

    head_sha = get_head_commit_sha()
    tracked_files = get_git_tracked_files()
    shards = build_And_compact_shards(tracked_files)

    if not shards:
        print(f"[{repo_name}] No eligible tracked text files found to sync.")
        return 0

    active_shard_names = sorted(shards.keys())
    print(f"[{repo_name}] Built {len(active_shard_names)} compacted shard(s) at commit {head_sha}:")

    for idx, shard_name in enumerate(active_shard_names):
        items = shards[shard_name]
        doc_text, shard_hash = build_shard_document(repo_name, shard_name, head_sha, items)
        is_last = (idx == len(active_shard_names) - 1)

        print(f"  -> Shard [{idx + 1}/{len(active_shard_names)}] '{shard_name}': "
              f"{len(items)} files, {len(doc_text):,} chars (hash: {shard_hash})")

        if dry_run:
            continue

        # Send activeShards list on the final shard so GAS can flag any removed folders as [STALE]
        try:
            res = post_shard_to_gas(
                webhook_url=webhook_url,
                secret=webhook_secret,
                repo_name=repo_name,
                shard_name=shard_name,
                content=doc_text,
                shard_hash=shard_hash,
                active_shards=active_shard_names if is_last else None
            )
            status = res.get("status")
            action = res.get("action", "unknown")
            if status != 200:
                print(f"     [ERROR] GAS returned status {status}: {res.get('error')}", file=sys.stderr)
                return 1
            print(f"     [OK] Action: {action} | Drive Doc ID: {res.get('fileId')}")
        except Exception as exc:
            print(f"     [FATAL] Failed pushing shard '{shard_name}': {exc}", file=sys.stderr)
            return 1

        # Brief pause between shard uploads to respect Drive API per-second burst limits
        if not is_last:
            time.sleep(1.0)

    print(f"[{repo_name}] All shards synchronized successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
