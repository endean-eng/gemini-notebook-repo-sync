#!/usr/bin/env python3
"""
notebook-sync.py
Zero-dependency GitHub-to-Google Drive Native Doc Bundler for Gemini Notebooks.

Features:
- Compatible with Python 3.8+ using 100% Python standard library modules.
- Null-delimited Git index extraction (`git ls-files -s -z`) with surrogate-safe
  UTF-8 path normalization and strict symlink (`120000`) / submodule (`160000`) exclusion.
- Segment-based directory exclusion (catches nested `node_modules/`, `dist/`, `.venv/`,
  etc. at any depth in monorepos), compound minified/sourcemap filtering (`.min.js`,
  `.min.css`, `.map`), and full-buffer null-byte binary rejection.
- Strict Root isolation: Root-level files ALWAYS occupy their own `00-ROOT` shard and
  never merge with subdirectories, even if the whole repo is <450k chars.
- All-or-Nothing sibling subtree coalescing with micro-leaf absorption (`<25k` chars)
  so peer modules stay symmetric and small edits do not churn Google Doc IDs.
- Pre-sanitized, case-insensitive collision-free shard keys (`__` hierarchy delimiter
  with `while` loop SHA-256 disambiguation) matching Google Drive's case-insensitive queries.
- Dynamic header overhead accounting and automatic clamping of `MAX_SINGLE_FILE_BYTES`
  against `MAX_SHARD_CHARS`.
- Dynamic N+1 backtick fence escaping (`re.findall(r"`+", raw_text)`).
- Custom HTTP redirect handler for Google Apps Script (`script.googleusercontent.com`)
  with bounded exponential backoff + jitter retries on HTTP 429/5xx and GAS 503 lock contention.
- Automatic empty-repo `[STALE]` sweep when all eligible tracked files are removed.
"""

import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

# ============================================================================
# CONFIGURATION & THRESHOLDS
# ============================================================================
FORMAT_VERSION = "v2"
MAX_SHARD_CHARS = max(10000, int(os.environ.get("MAX_SHARD_CHARS", "450000")))

# Dynamically clamp MAX_SINGLE_FILE_BYTES to 85% of MAX_SHARD_CHARS so a single file
# plus its wrapper/header never exceeds effective_ceiling when MAX_SHARD_CHARS is lowered.
_RAW_MAX_FILE_BYTES = int(os.environ.get("MAX_SINGLE_FILE_BYTES", "350000"))
MAX_SINGLE_FILE_BYTES = max(1024, min(_RAW_MAX_FILE_BYTES, int(MAX_SHARD_CHARS * 0.85)))

# Threshold for keeping substantial peer folders as standalone shards when an entire
# parent subtree cannot coalesce all-at-once into its parent.
MIN_STANDALONE_SHARD_CHARS = 25000

# Base character reserve for the shard document header (repo metadata + section title)
SHARD_HEADER_BASE_CHARS = 420

# Retry settings for GAS lock contention (503) or transient HTTP 429/5xx errors
MAX_POST_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 2.0

# Files and extensions excluded from Notebook RAG bundles (binaries, lockfiles, media, maps)
EXCLUDED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf",
    ".zip", ".tar", ".gz", ".7z", ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".mp4", ".mov", ".avi", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".class", ".o", ".obj", ".lock", ".sqlite", ".db",
    ".map", ".wasm", ".bin", ".dat", ".pkl", ".parquet"
}

EXCLUDED_COMPOUND_SUFFIXES = (
    ".min.js",
    ".min.mjs",
    ".min.cjs",
    ".min.css",
    ".bundle.js",
    ".chunk.js",
)

EXCLUDED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "Cargo.lock", "poetry.lock", "Pipfile.lock", "composer.lock",
    ".DS_Store", "Thumbs.db"
}

# Checked against every directory segment in a file's path (handles nested monorepos)
EXCLUDED_DIR_NAMES = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "out",
    ".next",
    ".nuxt",
    "coverage",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "target",
    "vendor",
}

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
# STEP 1: GIT INDEX & BLOB HASH EXTRACTION (-z NULL-DELIMITED & SURROGATE-SAFE)
# ============================================================================
def sanitize_utf8_str(val: str) -> str:
    """
    Replaces any lone surrogate code points produced by `surrogateescape` with
    U+FFFD so downstream `.encode("utf-8")` and `json.dumps()` calls never crash.
    """
    return val.encode("utf-8", errors="replace").decode("utf-8")


def sanitize_drive_token(val: str) -> str:
    """
    Mirrors `sanitizeName_` in GAS_web-app.js (`[\\/:*?"<>|]` and control chars -> `-`)
    so Python detects any sanitization-induced shard name collisions BEFORE upload.
    """
    clean = sanitize_utf8_str(val)
    clean = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", clean).strip()
    clean = re.sub(r"^-+|-+$", "", clean).strip()
    return clean or "unnamed"


def get_git_tracked_files() -> List[Tuple[str, str, str]]:
    """
    Returns a sorted list of (clean_rel_path, os_rel_path, git_blob_sha1) for all
    regular tracked files using `git ls-files -s -z`.
    Skips mode `160000` (submodules) and `120000` (symlinks) to prevent symlink
    dereferencing or host file leaks.
    """
    proc = subprocess.run(
        ["git", "ls-files", "-s", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=True
    )
    entries: List[Tuple[str, str, str]] = []
    for record in proc.stdout.split("\0"):
        if not record.strip():
            continue
        # Format: <mode> <blob_sha1> <stage>\t<path>
        meta, os_rel_path = record.split("\t", 1)
        parts = meta.split()
        mode, blob_sha = parts[0], parts[1]
        # Skip submodules (160000) and symlinks (120000)
        if mode in ("160000", "120000"):
            continue
        norm_os_path = os_rel_path.replace("\\", "/")
        clean_rel_path = sanitize_utf8_str(norm_os_path)
        entries.append((clean_rel_path, norm_os_path, blob_sha))
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


def read_and_validate_file(clean_rel_path: str, os_rel_path: str) -> Optional[str]:
    """
    Validates path exclusions, rejects symlinks and binary files (checking the
    entire buffer for null bytes), and returns decoded UTF-8 text if eligible.
    """
    path_obj = Path(os_rel_path)
    clean_parts = Path(clean_rel_path).parts

    # 1. Check directory segments at any depth (catches nested monorepo build/venv dirs)
    if len(clean_parts) > 1 and any(part in EXCLUDED_DIR_NAMES for part in clean_parts[:-1]):
        return None

    filename = clean_parts[-1] if clean_parts else ""
    filename_lower = filename.lower()

    if filename in EXCLUDED_FILENAMES:
        return None
    if any(filename_lower.endswith(sfx) for sfx in EXCLUDED_COMPOUND_SUFFIXES):
        return None
    if Path(filename_lower).suffix in EXCLUDED_EXTENSIONS:
        return None

    # 2. Never follow symlinks or special FIFOs/devices
    try:
        if path_obj.is_symlink() or not path_obj.is_file():
            return None
        size = path_obj.stat().st_size
        if size == 0 or size > MAX_SINGLE_FILE_BYTES:
            return None
        raw_bytes = path_obj.read_bytes()
    except OSError:
        return None

    # 3. Full-buffer null-byte check (prevents binary payloads after a 4KB text header)
    if not raw_bytes or b"\x00" in raw_bytes:
        return None

    return raw_bytes.decode("utf-8", errors="replace")


def compute_safe_fence(raw_text: str) -> str:
    """
    Computes the longest contiguous run of backticks in raw_text and returns
    a fence of length max(3, max_run + 1) so embedded code fences never break.
    """
    runs: List[str] = re.findall(r"`+", raw_text)
    max_run = max((len(r) for r in runs), default=0)
    return "`" * max(3, max_run + 1)


def manifest_entry_len(clean_rel_path: str) -> int:
    """
    Exact character length contributed by a file to the `## SHARD FILE MANIFEST`
    header section (`- `<clean_rel_path>` (git-blob: `<10-char-sha>`)\n`).
    """
    return len(clean_rel_path) + 30


def format_file_block(clean_rel_path: str, blob_sha: str, raw_text: str) -> str:
    """
    Formats a single file with an explicit path/hash header and dynamically
    sized fenced code block so Gemini Notebook RAG chunks retain file provenance.
    """
    line_count = raw_text.count("\n") + (1 if raw_text and not raw_text.endswith("\n") else 0)
    ext = Path(clean_rel_path).suffix.lower()
    lang = EXTENSION_TO_LANG.get(ext, "")

    fence = compute_safe_fence(raw_text)
    body = raw_text if raw_text.endswith("\n") else raw_text + "\n"

    return (
        f"\n# ============================================================================\n"
        f"# FILE: {clean_rel_path}\n"
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
    clean_rel_path, _, formatted_block = item
    return len(formatted_block) + manifest_entry_len(clean_rel_path)


# ============================================================================
# STEP 2: BOTTOM-UP DIRECTORY COMPACTION (WITH ALL-OR-NOTHING SIBLING COALESCING)
# ============================================================================
class DirNode:
    def __init__(self, path: str) -> None:
        self.path = path  # "" for root, "src/core" for subdirs
        self.children: Dict[str, "DirNode"] = {}
        # List of tuples: (clean_rel_path, blob_sha, formatted_block)
        self.files: List[Tuple[str, str, str]] = []

    @property
    def depth(self) -> int:
        if not self.path:
            return 0
        return len(self.path.split("/"))

    @property
    def direct_chars(self) -> int:
        return sum(item_footprint_chars(item) for item in self.files)


def allocate_unique_shard_key(
    raw_candidate: str,
    disambiguation_seed: str,
    reserved_lower: Set[str]
) -> str:
    """
    Sanitizes `raw_candidate` using the exact same rules as `GAS_web-app.js` and
    guarantees case-insensitive uniqueness across all shards in the repository
    (since Google Drive's `name = '...'` query operator is case-insensitive).
    """
    segments = [sanitize_drive_token(seg) for seg in raw_candidate.split("/") if seg]
    base_key = "__".join(segments) if segments else "00-ROOT"
    if not base_key.startswith(("00-", "01-")):
        base_key = f"01-{base_key}"

    candidate = base_key
    attempt = 0
    while candidate.lower() in reserved_lower:
        attempt += 1
        digest_input = f"{disambiguation_seed}:{attempt}".encode("utf-8", errors="replace")
        short_hash = hashlib.sha256(digest_input).hexdigest()[:6]
        candidate = f"{base_key}__{short_hash}"

    reserved_lower.add(candidate.lower())
    return candidate


def build_and_compact_shards(
    tracked_files: List[Tuple[str, str, str]],
    repo_name: str
) -> Dict[str, List[Tuple[str, str, str]]]:
    """
    1. Groups files by exact parent directory.
    2. Isolates root files (`path == ""`) into `00-ROOT` unconditionally.
    3. Walks non-root parent directories bottom-up (from `max_depth - 1` down to `1`):
       - All-or-Nothing Subtree Coalescence: If a parent directory plus ALL of its
         non-empty child directories fit within `effective_ceiling`, all children
         coalesce cleanly into the parent.
       - Symmetric Peer Preservation: If the combined subtree exceeds `effective_ceiling`,
         substantial child modules (`>= MIN_STANDALONE_SHARD_CHARS`) freeze as their own
         dedicated shards so peer modules are never asymmetrically stranded, while tiny
         leaf folders (`< MIN_STANDALONE_SHARD_CHARS`) merge into the parent if they fit.
    4. Splits any single oversized shard into collision-free `__partN` shards.
    """
    header_reserve = SHARD_HEADER_BASE_CHARS + len(repo_name) * 2
    effective_ceiling = max(2000, MAX_SHARD_CHARS - header_reserve)

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
    for clean_rel_path, os_rel_path, blob_sha in tracked_files:
        raw_text = read_and_validate_file(clean_rel_path, os_rel_path)
        if raw_text is None:
            continue
        formatted_block = format_file_block(clean_rel_path, blob_sha, raw_text)
        parent_dir = os.path.dirname(clean_rel_path).replace("\\", "/")
        node = get_or_create_node(parent_dir)
        node.files.append((clean_rel_path, blob_sha, formatted_block))

    finalized_shards: Dict[str, List[Tuple[str, str, str]]] = {}
    reserved_lower: Set[str] = set()

    # Rule A: Root files ALWAYS get their own dedicated shard(s), never merged with subdirs
    if root.files:
        root_key = allocate_unique_shard_key("00-ROOT", "root", reserved_lower)
        finalized_shards[root_key] = sorted(root.files, key=lambda x: x[0])
        root.files = []

    # Rule B: Bottom-up parent-centric compaction for subdirectories (depth >= 1)
    max_depth = max((n.depth for p, n in nodes_by_path.items() if p != ""), default=0)

    for parent_depth in range(max_depth - 1, 0, -1):
        parents_at_depth = sorted(
            (n for p, n in nodes_by_path.items() if p != "" and n.depth == parent_depth),
            key=lambda n: n.path
        )
        for parent_node in parents_at_depth:
            active_children = [c for c in parent_node.children.values() if c.files]
            if not active_children:
                continue

            total_children_chars = sum(c.direct_chars for c in active_children)

            # Case 1: Entire subtree (parent + all children) fits -> coalesce all children
            if (parent_node.direct_chars + total_children_chars) <= effective_ceiling:
                for child in sorted(active_children, key=lambda c: c.path):
                    parent_node.files.extend(child.files)
                    child.files = []
                continue

            # Case 2: Subtree exceeds ceiling. Freeze substantial peer modules (>= 25k chars)
            # as standalone shards so peer siblings remain symmetric, and absorb only tiny
            # leaf folders (< 25k chars) if they fit into parent_node.
            active_children.sort(key=lambda c: (c.direct_chars, c.path))
            for child in active_children:
                if (
                    child.direct_chars < MIN_STANDALONE_SHARD_CHARS
                    and (parent_node.direct_chars + child.direct_chars) <= effective_ceiling
                ):
                    parent_node.files.extend(child.files)
                    child.files = []
                else:
                    shard_key = allocate_unique_shard_key(
                        f"01-{child.path}", child.path, reserved_lower
                    )
                    finalized_shards[shard_key] = sorted(child.files, key=lambda x: x[0])
                    child.files = []

    # Freeze remaining Depth == 1 top-level directories
    depth_one_nodes = sorted(
        (n for p, n in nodes_by_path.items() if p != "" and n.depth == 1 and n.files),
        key=lambda n: n.path
    )
    for top_node in depth_one_nodes:
        shard_key = allocate_unique_shard_key(
            f"01-{top_node.path}", top_node.path, reserved_lower
        )
        finalized_shards[shard_key] = sorted(top_node.files, key=lambda x: x[0])
        top_node.files = []

    # Rule C: Split any shard that exceeds effective_ceiling into collision-free `__partN` shards
    size_bounded_shards: Dict[str, List[Tuple[str, str, str]]] = {}
    for shard_key in sorted(finalized_shards.keys()):
        items = finalized_shards[shard_key]
        total_chars = sum(item_footprint_chars(x) for x in items)
        if total_chars <= effective_ceiling or len(items) <= 1:
            size_bounded_shards[shard_key] = items
        else:
            buckets: List[List[Tuple[str, str, str]]] = []
            current_bucket: List[Tuple[str, str, str]] = []
            current_chars = 0
            for item in items:
                item_len = item_footprint_chars(item)
                if current_bucket and (current_chars + item_len) > effective_ceiling:
                    buckets.append(current_bucket)
                    current_bucket = [item]
                    current_chars = item_len
                else:
                    current_bucket.append(item)
                    current_chars += item_len
            if current_bucket:
                buckets.append(current_bucket)

            for part_idx, bucket in enumerate(buckets, start=1):
                raw_part_key = f"{shard_key}__part{part_idx}"
                part_key = allocate_unique_shard_key(
                    raw_part_key, f"{shard_key}:part{part_idx}", reserved_lower
                )
                size_bounded_shards[part_key] = bucket

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
    The composite hash includes FORMAT_VERSION, repo_name, shard_name, and every
    member file's (clean_rel_path + git_blob_sha1), while excluding `head_sha` so
    unchanged shards skip uploads across commits.
    """
    hasher = hashlib.sha256()
    hasher.update(f"format:{FORMAT_VERSION}|repo:{repo_name}|shard:{shard_name}\n".encode("utf-8"))

    manifest_lines: List[str] = []
    for clean_rel_path, blob_sha, _ in items:
        hasher.update(f"{clean_rel_path}:{blob_sha}\n".encode("utf-8"))
        manifest_lines.append(f"- `{clean_rel_path}` (git-blob: `{blob_sha[:10]}`)")

    shard_hash = hasher.hexdigest()[:16]

    header = (
        f"# REPOSITORY: {repo_name}\n"
        f"# SHARD: {shard_name}\n"
        f"# SHARD_LAST_MODIFIED_COMMIT: {head_sha}\n"
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
    1. Converts post-`doPost` 302 redirects to `*.googleusercontent.com` into clean
       GET requests with `Content-Type` and `Content-Length` removed.
    2. Rejects any other 301/302/303 redirect on a POST request to prevent silent
       POST body stripping on misconfigured `/dev` or OAuth URLs.
    """
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str
    ) -> Optional[urllib.request.Request]:
        parsed = urllib.parse.urlparse(newurl)
        host = (parsed.hostname or "").lower()

        if code in (301, 302, 303) and req.get_method() == "POST":
            if host.endswith("googleusercontent.com"):
                clean_headers: Dict[str, str] = {
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

        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]


def post_shard_to_gas(
    webhook_url: str,
    secret: str,
    repo_name: str,
    shard_name: str,
    content: str,
    shard_hash: str,
    active_shards: Optional[List[str]],
    sync_stale_only: bool = False
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "secret": secret,
        "repo": repo_name,
        "shardName": shard_name,
        "shardHash": shard_hash,
        "content": content,
        "syncStaleOnly": sync_stale_only,
    }
    if active_shards is not None:
        payload["activeShards"] = active_shards

    data = json.dumps(payload).encode("utf-8")
    opener = urllib.request.build_opener(GASRedirectHandler())

    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_POST_ATTEMPTS + 1):
        raw_resp = ""
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with opener.open(req, timeout=90) as resp:
                raw_resp = resp.read().decode("utf-8", errors="replace")
            res: Dict[str, Any] = json.loads(raw_resp)
            # Retry on GAS script lock contention (status 503 inside JSON response)
            if res.get("status") == 503 and attempt < MAX_POST_ATTEMPTS:
                sleep_s = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0.1, 0.9)
                print(f"     [WARN] GAS lock busy (503) on attempt {attempt}/{MAX_POST_ATTEMPTS}; "
                      f"retrying in {sleep_s:.1f}s...")
                time.sleep(sleep_s)
                continue
            return res

        except urllib.error.HTTPError as http_err:
            err_body = ""
            try:
                err_body = http_err.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            if http_err.code in (429, 500, 502, 503, 504) and attempt < MAX_POST_ATTEMPTS:
                sleep_s = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0.1, 0.9)
                print(f"     [WARN] HTTP {http_err.code} on attempt {attempt}/{MAX_POST_ATTEMPTS}; "
                      f"retrying in {sleep_s:.1f}s...")
                time.sleep(sleep_s)
                last_err = http_err
                continue
            raise RuntimeError(
                f"HTTP {http_err.code} ({http_err.reason}) from GAS webhook. Response body: {err_body}"
            ) from http_err

        except (urllib.error.URLError, TimeoutError, ConnectionError) as net_err:
            if attempt < MAX_POST_ATTEMPTS and "Refusing to drop POST payload" not in str(net_err):
                sleep_s = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0.1, 0.9)
                print(f"     [WARN] Network error ({net_err}) on attempt {attempt}/{MAX_POST_ATTEMPTS}; "
                      f"retrying in {sleep_s:.1f}s...")
                time.sleep(sleep_s)
                last_err = net_err
                continue
            raise RuntimeError(f"Network failure calling GAS webhook: {net_err}") from net_err

        except json.JSONDecodeError as dec_err:
            snippet = raw_resp[:300].replace("\n", " ")
            raise RuntimeError(
                f"Non-JSON response from GAS webhook (check Web App deployment permissions): {snippet}"
            ) from dec_err

    raise RuntimeError(f"Exhausted {MAX_POST_ATTEMPTS} attempts calling GAS webhook: {last_err}")


def main() -> int:
    webhook_url = os.environ.get("GAS_WEBHOOK_URL", "").strip()
    webhook_secret = os.environ.get("GAS_WEBHOOK_SECRET", "").strip()

    raw_repo = os.environ.get("REPO_NAME") or os.environ.get("GITHUB_REPOSITORY", "")
    extracted_repo = raw_repo.split("/")[-1].strip() if raw_repo else Path.cwd().name
    repo_name = sanitize_drive_token(extracted_repo) if extracted_repo else "unknown-repo"

    dry_run = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

    if not dry_run and (not webhook_url or not webhook_secret):
        print("ERROR: GAS_WEBHOOK_URL and GAS_WEBHOOK_SECRET environment variables are required.", file=sys.stderr)
        return 1

    head_sha = get_head_commit_sha()
    tracked_files = get_git_tracked_files()
    shards = build_and_compact_shards(tracked_files, repo_name)

    if not shards:
        print(f"[{repo_name}] No eligible tracked text files found.")
        if not dry_run:
            try:
                res = post_shard_to_gas(
                    webhook_url=webhook_url,
                    secret=webhook_secret,
                    repo_name=repo_name,
                    shard_name="00-EMPTY",
                    content="",
                    shard_hash="",
                    active_shards=[],
                    sync_stale_only=True
                )
                print(f"[{repo_name}] Marked any previous Drive documents as [STALE] (status: {res.get('status')}).")
            except Exception as exc:
                print(f"     [FATAL] Failed running empty-repo [STALE] sweep: {exc}", file=sys.stderr)
                return 1
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

        if not is_last:
            time.sleep(0.3)

    print(f"[{repo_name}] All shards synchronized successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())