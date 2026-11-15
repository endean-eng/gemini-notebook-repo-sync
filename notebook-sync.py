#!/usr/bin/env python3
"""
notebook-sync.py
Zero-dependency GitHub-to-Google Drive Native Doc Bundler for Gemini Notebooks.

Features:
- Null-delimited Git index extraction (`git ls-files -s -z`) for full UTF-8 / special
  character path fidelity without C-style octal quoting issues.
- Strict Root isolation: Root-level files ALWAYS occupy their own `00-ROOT` shard and
  never merge with subdirectories, even if the whole repo is <450k chars.
- Collision-resistant hierarchy flattening (`__` path separator) and smallest-first
  sibling compaction (`direct_chars` ascending) to prevent large-child starvation.
- Accounts for document header and per-file manifest line overhead in all character
  limit calculations (`MAX_SHARD_CHARS`).
- Dynamic N+1 backtick fence escaping (`re.findall(r"`+", raw_text)`) so files containing
  3, 4, or more consecutive backticks never break outer Markdown code blocks.
- Custom HTTP redirect handler for Google Apps Script (`script.googleusercontent.com`)
  that strips `Content-Type` / `Content-Length` on 302 output redirects while rejecting
  premature POST-dropping redirects.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ============================================================================
# CONFIGURATION & THRESHOLDS
# ============================================================================
MAX_SHARD_CHARS = int(os.environ.get("MAX_SHARD_CHARS", "450000"))
MAX_SINGLE_FILE_BYTES = int(os.environ.get("MAX_SINGLE_FILE_BYTES", "350000"))

# Base character reserve for the shard document header (repo metadata + section title)
SHARD_HEADER_BASE_CHARS = 320

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
# STEP 1: GIT INDEX & BLOB HASH EXTRACTION (-z NULL-DELIMITED)
# ============================================================================
def get_git_tracked_files() -> List[Tuple[str, str]]:
    """
    Returns a sorted list of (rel_path, git_blob_sha1) for all tracked files
    using `git ls-files -s -z` to prevent C-style path quoting on UTF-8 paths.
    """
    proc = subprocess.run(
        ["git", "ls-files", "-s", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=True
    )
    entries: List[Tuple[str, str]] = []
    for record in proc.stdout.split("\0"):
        if not record.strip():
            continue
        # Format: <mode> <blob_sha1> <stage>\t<path>
        meta, rel_path = record.split("\t", 1)
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


def compute_safe_fence(raw_text: str) -> str:
    """
    Computes the longest contiguous run of backticks in raw_text and returns
    a fence of length max(3, max_run + 1) so embedded code fences never break.
    """
    runs = re.findall(r"`+", raw_text)
    max_run = max((len(r) for r in runs), default=0)
    return "`" * max(3, max_run + 1)


def manifest_entry_len(rel_path: str) -> int:
    """
    Exact character length contributed by a file to the `## SHARD FILE MANIFEST`
    header section (`- \`<rel_path>\` (git-blob: \`<10-char-sha>\`)\n`).
    """
    return len(rel_path) + 30


def format_file_block(rel_path: str, blob_sha: str) -> str:
    """
    Formats a single file with an explicit path/hash header and dynamically
    sized fenced code block so Gemini Notebook RAG chunks retain file provenance.
    """
    path_obj = Path(rel_path)
    raw_text = path_obj.read_text(encoding="utf-8", errors="replace")
    line_count = raw_text.count("\n") + (1 if raw_text and not raw_text.endswith("\n") else 0)
    ext = path_obj.suffix.lower()
    lang = EXTENSION_TO_LANG.get(ext, "")

    fence = compute_safe_fence(raw_text)
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


def item_footprint_chars(item: Tuple[str, str, str]) -> int:
    """
    Total character footprint of a file item inside a shard document:
    formatted code block length + manifest line length.
    """
    rel_path, _, formatted_block = item
    return len(formatted_block) + manifest_entry_len(rel_path)


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
        return sum(item_footprint_chars(item) for item in self.files)


def dir_path_to_shard_key(dir_path: str, existing_shards: Dict[str, List[Tuple[str, str, str]]]) -> str:
    """
    Converts a relative directory path into a collision-free shard key using `__`
    as the hierarchy separator (so `src/core-utils` -> `01-src__core-utils` and
    `src/core/utils` -> `01-src__core__utils`). If an edge-case collision still
    occurs (e.g., a folder literally named `core__utils`), appends a 6-char path hash.
    """
    base_key = f"01-{dir_path.replace('/', '__')}"
    if base_key not in existing_shards:
        return base_key
    short_hash = hashlib.sha256(dir_path.encode("utf-8")).hexdigest()[:6]
    return f"{base_key}__{short_hash}"


def build_and_compact_shards(
    tracked_files: List[Tuple[str, str]]
) -> Dict[str, List[Tuple[str, str, str]]]:
    """
    1. Groups files by exact parent directory.
    2. Isolates root files (`path == ""`) into `00-ROOT` unconditionally.
    3. Walks non-root directories bottom-up (deepest first, smallest `direct_chars`
       first among siblings at the same depth):
       - Empty pass-through directories (0 files) produce no shard.
       - Leaf directories merge upward into their parent directory if their
         combined size (including header & manifest overhead) stays <= MAX_SHARD_CHARS,
         stopping at Depth 1 (top-level subdirectories like `src/`, `docs/`, `tests/`).
    """
    effective_ceiling = max(2000, MAX_SHARD_CHARS - SHARD_HEADER_BASE_CHARS)

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

    # Rule B: Bottom-up compaction of all subdirectories (depth >= 1).
    # Process level-by-level from deepest depth down to depth 1. Within each depth,
    # sort by (direct_chars ASC, path ASC) so small leaf directories coalesce into
    # their parent first while large self-contained modules freeze as dedicated shards.
    max_depth = max((n.depth for p, n in nodes_by_path.items() if p != ""), default=0)

    for current_depth in range(max_depth, 0, -1):
        level_nodes = [
            n for p, n in nodes_by_path.items()
            if p != "" and n.depth == current_depth
        ]
        level_nodes.sort(key=lambda n: (n.direct_chars, n.path))

        for node in level_nodes:
            if not node.files:
                continue

            if node.depth > 1:
                parent_path = os.path.dirname(node.path).replace("\\", "/")
                parent_node = nodes_by_path[parent_path]

                if (parent_node.direct_chars + node.direct_chars) <= effective_ceiling:
                    parent_node.files.extend(node.files)
                    node.files = []
                else:
                    shard_key = dir_path_to_shard_key(node.path, finalized_shards)
                    finalized_shards[shard_key] = sorted(node.files, key=lambda x: x[0])
                    node.files = []
            else:
                # Depth == 1 (Top-level folder like `src`, `docs`, `scripts`)
                # Never merges into root! Freeze it as a top-level shard.
                shard_key = dir_path_to_shard_key(node.path, finalized_shards)
                finalized_shards[shard_key] = sorted(node.files, key=lambda x: x[0])
                node.files = []

    # Rule C: Split any shard that exceeds effective_ceiling (e.g. a single massive flat folder)
    size_bounded_shards: Dict[str, List[Tuple[str, str, str]]] = {}
    for shard_key in sorted(finalized_shards.keys()):
        items = finalized_shards[shard_key]
        total_chars = sum(item_footprint_chars(x) for x in items)
        if total_chars <= effective_ceiling or len(items) <= 1:
            size_bounded_shards[shard_key] = items
        else:
            part_idx = 1
            current_bucket: List[Tuple[str, str, str]] = []
            current_chars = 0
            for item in items:
                item_len = item_footprint_chars(item)
                if current_bucket and (current_chars + item_len) > effective_ceiling:
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


class GASRedirectHandler(urllib.request.HTTPRedirectHandler):
    """
    Custom redirect handler for Google Apps Script Web Apps:
    1. When `doPost(e)` finishes executing on `script.google.com`, Google returns an
       HTTP 302 redirect to `script.googleusercontent.com` to fetch the JSON output.
       Standard `urllib` keeps `Content-Type: application/json` on the redirected GET
       request, which can trigger HTTP 400/405 responses from Google's CDN. We strip
       entity headers and convert to a clean GET for `*.googleusercontent.com`.
    2. If a 301/302/303 redirect points anywhere else (e.g., an enterprise Workspace
       domain login or a misconfigured non-`/exec` URL), converting POST to GET would
       silently drop the POST body before `doPost(e)` ever runs. We raise an explicit
       error explaining the redirect target.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        host = (parsed.hostname or "").lower()

        if code in (301, 302, 303) and req.get_method() == "POST":
            if host.endswith("googleusercontent.com"):
                clean_headers = {
                    k: v for k, v in req.header_items()
                    if k.lower() not in ("content-type", "content-length")
                }
                return urllib.request.Request(
                    newurl,
                    data=None,
                    headers=clean_headers,
                    method="GET"
                )
            raise urllib.error.URLError(
                f"Refusing to drop POST payload on HTTP {code} redirect to '{newurl}'. "
                f"Verify that GAS_WEBHOOK_URL ends with '/exec' and 'Who has access' is set to 'Anyone'."
            )

        return super().redirect_request(req, fp, code, msg, headers, newurl)


def post_shard_to_gas(
    webhook_url: str,
    secret: str,
    repo_name: str,
    shard_name: str,
    content: str,
    shard_hash: str,
    active_shards: Optional[List[str]]
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

    opener = urllib.request.build_opener(GASRedirectHandler())
    try:
        with opener.open(req, timeout=90) as resp:
            raw_resp = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as http_err:
        err_body = ""
        try:
            err_body = http_err.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(
            f"HTTP {http_err.code} ({http_err.reason}) from GAS webhook. Response body: {err_body}"
        ) from http_err

    try:
        return json.loads(raw_resp)
    except json.JSONDecodeError as dec_err:
        snippet = raw_resp[:300].replace("\n", " ")
        raise RuntimeError(
            f"Non-JSON response from GAS webhook (check Web App deployment permissions): {snippet}"
        ) from dec_err


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
    shards = build_and_compact_shards(tracked_files)

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
