# Live GitHub-to-Gemini Notebook Sync

**Zero-daemon, automated GitHub repository synchronization into native Google Docs for live context inside Gemini Notebooks (NotebookLM).**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](#license)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![Dependencies: Zero](https://img.shields.io/badge/dependencies-zero%20(stdlib)-brightgreen.svg)](#supply-chain-minimalism--5-minute-auditability)
[![Google Apps Script](https://img.shields.io/badge/Google%20Apps%20Script-V8-green.svg)](https://script.google.com/)

---

## Supply-Chain Minimalism & 5-Minute Auditability

Modern CI/CD utilities routinely pull dozens of transitive `pip` or `npm` packages just to walk a directory tree or send an HTTP request—introducing Dependabot churn and upstream supply-chain exposure into workflows that have read access to your source tree.

This project is engineered around **strict supply-chain minimalism and deterministic verification**:
* **Zero External Dependencies (`stdlib` Only):** `notebook-sync.py` uses 100% Python standard library modules (`hashlib`, `json`, `os`, `random`, `re`, `subprocess`, `urllib.request`, `pathlib`) and runs natively on any Python 3.8+ interpreter. No `pip install` step is ever executed.
* **5-Minute Human Auditability:** The entire system consists of ~470 lines of Python (`notebook-sync.py`) and ~240 lines of Google Apps Script (`GAS_web-app.js`). A security or staff engineer can audit every line of code, network call, and file-system read in under 5 minutes.
* **Least-Privilege CI Execution:** Runs strictly under `permissions: contents: read` with zero write access to your repository, packages, or pull requests, skips all Git symlinks (`120000`) and submodules (`160000`), and binds all inputs via environment variables (`env:`) to eliminate shell-injection vectors.

---

## The Problem

Keeping a codebase synchronized with a Gemini Web UI Notebook (or NotebookLM) normally runs into three major architectural roadblocks:

1. **Git Corruption via Desktop Sync Clients:** Pointing the Google Drive for Desktop client directly at a local Git repository causes file-locking collisions on `.git/index.lock` and loose objects, corrupting local commit graphs while uploading `node_modules/` bloat.
2. **The Native MIME-Type Gate:** Gemini Notebooks only auto-sync native Google Workspace documents (`application/vnd.google-apps.document`). Raw `.js`, `.ts`, `.py`, or `.md` files uploaded to Drive are either rejected by file-extension filters or ingested once as static, frozen snapshots that never auto-refresh.
3. **Single-Document Context Limits:** Dumping an entire repository into a single file hits Google Docs' 1.02M character ceiling and degrades Retrieval-Augmented Generation (RAG) precision.

## The Solution

This project bridges GitHub Actions directly to a lightweight, self-hosted **Google Apps Script Web App (`doPost`)** running under your own Google account—eliminating local background processes, GCP Service Account IAM complexity, and third-party servers.

```mermaid
flowchart TD
    Push["VS Code: git push"] --> GHA["GitHub Actions: notebook-sync.py"]

    subgraph Bundler ["1. Local Git Bundler & Compactor (GitHub Runner)"]
        GHA --> GitIdx["Extract null-delimited index<br/>(git ls-files -s -z, skip symlinks)"]
        GitIdx --> RootIso["Isolate 00-ROOT & coalesce leaf subtrees<br/>all-or-nothing (<= 450k chars)"]
        RootIso --> PostShard["Send 1 HTTP POST per shard<br/>(exponential backoff & 302 handler)"]
    end

    PostShard --> GAS["Google Apps Script Web App (doPost)"]

    subgraph Receiver ["2. Serverless Drive API v3 Mutator (Google Cloud)"]
        GAS --> HashCheck{"Matches existing<br/>git-shard-hash?"}
        HashCheck -- "Yes (Unchanged)" --> Skip["Skip upload in ~100ms<br/>(strip [STALE] if restored)"]
        HashCheck -- "No (Modified / New)" --> Upsert["Drive.Files.update / create<br/>(preserves native Doc FILE_ID)"]
    end

    Skip --> DriveFolder["Google Drive: Gemini-Notebook-Mirrors/&lt;repo&gt;/"]
    Upsert --> DriveFolder
    DriveFolder --> Notebook["Gemini Web UI Notebook<br/>(Auto-syncs native Docs & folder checkboxes)"]
```

---

## System Operating Envelope & Architectural Decision Ledger

| Physical / API Constraint | Architectural Decision | Rejected Alternative & Failure Mode | Safety Factor / Operational Bound |
|---|---|---|---|
| **Google Docs 1.02M Char Hard Cap & GAS 100MB V8 Heap** | Enforce `MAX_SHARD_CHARS = 450,000` (inclusive of dynamic header & manifest overhead), clamp `MAX_SINGLE_FILE_BYTES <= 0.85 * MAX_SHARD_CHARS`, and send **1 HTTP `POST` per shard**. | Sending the entire repository in a single `doPost` JSON payload (50MB ingress limit), which exhausts the 100MB V8 heap during `JSON.parse`. | **2.26x safety margin** below the 1.02M char limit; keeps V8 UTF-16 string allocation under 5% of heap capacity and resets the 6-minute execution clock per shard. |
| **GAS HTTP 302 Redirects & Multi-Repo Script Lock Contention** | Custom `GASRedirectHandler` strips `Content-Type` on `*.googleusercontent.com` output redirects, paired with 4-attempt exponential backoff + jitter on HTTP `429/5xx` and GAS `503` lock timeouts. | Default `urllib.request.urlopen` (which retains `Content-Type` on redirected GETs or drops POST bodies) with zero retry on multi-repo lock contention. | **100% payload integrity** across concurrent Organization-wide repository pushes. |
| **Drive API Mutation Latency & Notebook Link Persistence** | Stream `text/plain` media payloads into native Docs via Drive API v3 `Drive.Files.update`. | Mutating text via `DocumentApp.openById().getBody().setText()`, or deleting/re-creating files on each push. | **~800ms update latency** (vs. DOM parsing timeouts on >200KB files) while preserving the exact Google Drive `FILE_ID` linked in Gemini Notebook. |
| **Storage Quota Ownership** | Deploy Apps Script Web App as `Execute as: Me` gated by a constant-time verified secret (`timingSafeEqual_`). | Provisioning a GCP Service Account per repo or organization. | **Zero service-account quota traps**; avoids invisible 15GB Service Account Drive quota exhaustion (`storageQuotaExceeded`) and IAM key rotation. |
| **RAG Context Stability & Case-Insensitive Drive Queries** | Unconditional `00-ROOT` isolation + **All-or-Nothing sibling subtree coalescing** + pre-sanitized, case-insensitive collision-free `__` shard keys. | Greedy pairwise sibling merging (which strands peer folders when two siblings fill the parent) or case-sensitive keys (which collide in Drive's case-insensitive `name =` query). | **Zero path or casing collisions**, symmetric peer module shards, and clean top-level checkboxes in Gemini Notebook. |

---

## Key Features

* **Preserved Google Drive `FILE_ID`s:** Updates overwrite the body of existing native Google Docs in-place via Drive API v3 (`Drive.Files.update`), so your Gemini Notebook never loses its source link.
* **Strict Root Isolation & All-or-Nothing Sibling Compaction:**
  * Root-level files (`README.md`, `package.json`, configs) always stay isolated in their own `<repo>__00-ROOT` document, even if the entire repository is well under the character limit.
  * Pass-through directories containing only subdirectories (0 direct files) are pruned automatically and never create empty Google Docs.
  * When a parent directory and **all** of its child subdirectories fit within 450,000 characters, the entire subtree coalesces cleanly into the parent shard. If the combined subtree exceeds 450,000 characters, substantial peer subdirectories (`>= 25,000` chars) remain standalone shards while tiny helper folders (`< 25,000` chars) merge into the parent—preventing sibling starvation and avoiding Google Doc ID churn when individual files grow.
  * Flattened shard names are pre-sanitized and checked case-insensitively with double-underscore hierarchy separators (`01-src__core__utils`) and automatic SHA-256 disambiguation if two directories ever collide.
* **Monorepo & Binary Hardening:** Automatically skips Git symlinks (`120000`), submodules (`160000`), nested build/virtualenv directories at any depth (`node_modules/`, `dist/`, `build/`, `.venv/`, `__pycache__/`), minified bundles (`.min.js`, `.min.css`, `.map`), and files containing null bytes anywhere in their payload.
* **Dynamic N+1 Backtick Fence Escaping:** Inspects every source file for contiguous backtick sequences (`re.findall(r"`+", raw_text)`) and wraps the file in an outer fence of `max(3, max_run + 1)` backticks—ensuring Markdown files and scripts that contain triple or quadruple backticks never prematurely terminate their code block.
* **Git Blob SHA-1 Delta Skipping:** Computes a deterministic composite hash (`FORMAT_VERSION` + `repo_name` + `shard_name` + member file `rel_path:git_blob_sha1`) and stores it in the Google Doc's `description` metadata. Unchanged shards are skipped in ~100ms without re-uploading document bodies.
* **Safe Refactoring (`[STALE]` Flagging, Auto-Restore & Empty-Repo Sweep):** Orphaned Google Docs are prefixed with `[STALE]` (paginated across folders of any size), automatically restored if the folder becomes active again, and swept cleanly even if all eligible text files are removed from a repository.

---

## Repository Contents

* `action.yml` — Composite GitHub Action definition enabling 1-line `uses:` integration across multiple repositories.
* `GAS_web-app.js` — The Google Apps Script Web App receiver (`Code.gs`).
* `notebook-sync.py` — Zero-dependency Python 3 bundler and bottom-up directory sharding engine.
* `notebook-sync.yml` — Drop-in GitHub Actions workflow template (supports both `.github/scripts/notebook-sync.py` and root `notebook-sync.py`).
* `LICENSE` — MIT License.

---

## Step-by-Step Initialization & Multi-Repo Setup Guide

### Phase 1: One-Time Google Apps Script Web App Deployment (5 Minutes)

You only perform this phase **once** across your entire Google account. All of your current and future GitHub repositories will reuse this single endpoint.

1. **Create the Script Project:**
   * Open [script.google.com](https://script.google.com) and click **New project**.
   * Rename the project at the top to `Gemini Notebook Repo Sync`.

2. **Enable the Drive API v3 Service (CRITICAL):**
   * In the left-hand sidebar of the Apps Script editor, click the **`+` next to Services**.
   * Scroll down and select **Drive API**.
   * Ensure the Version is set to **`v3`** and the Identifier is **`Drive`**.
   * Click **Add**. *(Without this step, `Drive.Files.create` and `Drive.Files.update` will fail with `ReferenceError: Drive is not defined`.)*

3. **Paste the Webhook Code & Set Your Secret:**
   * Copy the contents of `GAS_web-app.js` and paste them into `Code.gs` (replacing the default `myFunction`).
   * Generate a random secret token (for example, run `openssl rand -hex 24` in any terminal, or type a strong 32+ character passphrase).
   * Replace `"REPLACE_WITH_YOUR_SECRET_TOKEN"` on line 12 of `Code.gs` with your secret token:
     ```javascript
     const SECRET_TOKEN = "your-generated-secret-token";
     ```
     *(Note: The script actively rejects requests with HTTP 500 if `SECRET_TOKEN` is left at the default placeholder.)*
   * Press **Ctrl+S** (or click the Save icon).

4. **Deploy & Authorize the Web App:**
   * In the top right corner, click **Deploy** -> **New deployment**.
   * Click the gear icon next to "Select type" and choose **Web app**.
   * Configure the fields exactly as follows:
     * **Description:** `v1 Production Receiver`
     * **Execute as:** **`Me (your-email@gmail.com)`** *(Ensures all folders and Docs are created directly in your Drive using your storage quota rather than a capped service account.)*
     * **Who has access:** **`Anyone`** *(Required so headless GitHub Actions runners can POST to the URL without interactive OAuth browser redirects; requests are gated by your `SECRET_TOKEN`.)*
   * Click **Deploy**.
   * **Completing the "Unverified App" OAuth Prompt:**
     1. Click **Authorize access** and select your Google account.
     2. Google will show a "Google hasn't verified this app" warning screen because you just wrote the script yourself.
     3. Click **Advanced** (small link at the bottom left).
     4. Click **Go to Gemini Notebook Repo Sync (unsafe)** at the bottom.
     5. Click **Allow**.
   * Copy the generated **Web app URL** (`https://script.google.com/macros/s/.../exec`) and keep it handy for Phase 2.

---

### Phase 2: Configure GitHub Secrets (Per-Repo or Account-Wide)

1. Open your target GitHub repository -> **Settings** -> **Secrets and variables** -> **Actions**.
   * **Multi-Repo Scaling Pattern:** If your repositories live under a GitHub Organization, add these two secrets once under **Organization Settings -> Secrets and variables -> Actions** with repository access set to **All repositories**. Every new repo you create will have instant access with zero secret configuration.
2. Click **New repository secret** and add:
   * **Name:** `GAS_WEBHOOK_URL`
   * **Secret:** *(Paste the `https://script.google.com/macros/s/.../exec` URL from Phase 1)*
3. Click **New repository secret** again and add:
   * **Name:** `GAS_WEBHOOK_SECRET`
   * **Secret:** *(Paste the exact `SECRET_TOKEN` string you placed in `Code.gs`)*

---

### Phase 3: Connect Your Repository (Choose Pattern A or Pattern B)

You can integrate the bundler into your repositories using either the **Composite Action (`action.yml`)** for centralized multi-repo management or the **Vendored Script** pattern for self-contained repositories.

| Criteria | Pattern A: Composite Action (`action.yml`) | Pattern B: Vendored Script (`notebook-sync.py`) |
|---|---|---|
| **How It Works** | GitHub runner checks out your target repo in `$GITHUB_WORKSPACE`, clones this tool repo into `${{ github.action_path }}`, and runs `notebook-sync.py` using the runner's `python3` against your working tree. | You copy `notebook-sync.py` directly into `.github/scripts/notebook-sync.py` inside the target repository. |
| **When to Use** | Best when syncing **multiple repositories** (avoids duplicating `notebook-sync.py` across 5–20 repos). | Best for **air-gapped / zero-external-action environments** or when customizing sharding rules for a single repo. |
| **Supply-Chain Security** | Pin the `uses:` reference to a full **40-character commit SHA** so upstream tag mutations cannot affect your workflow. | 100% local to your repository; code only changes when you commit to that repo. |

---

#### Pattern A: Using the Composite Action (`action.yml`) — Recommended for Multi-Repo Fleets

Create `.github/workflows/notebook-sync.yml` in your target repository (replace `<YOUR_GITHUB_ORG_OR_USER>/<THIS_REPO_NAME>@<40_CHAR_COMMIT_SHA>` with the commit hash of your deployed tool repository):

```yaml
name: Sync Repo to Gemini Notebook (Google Drive)

on:
  push:
    branches:
      - main
      - master
  workflow_dispatch:

# Least-privilege token scope: read-only access to repository contents
permissions:
  contents: read

concurrency:
  group: notebook-drive-sync-${{ github.repository }}
  cancel-in-progress: true

jobs:
  sync-to-drive:
    runs-on: ubuntu-latest
    timeout-minutes: 15

    steps:
      - name: Checkout Target Repository
        uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          fetch-depth: 1

      - name: Shard & Sync to Google Drive via Composite Action
        uses: endean-eng/gemini-notebook-repo-sync@3c8d77fc8a045caffcfb778e80e7a1dc3ce0fc70 # v1.0.0
        with:
          gas-webhook-url: ${{ secrets.GAS_WEBHOOK_URL }}
          gas-webhook-secret: ${{ secrets.GAS_WEBHOOK_SECRET }}
          # Optional inputs:
          # repo-name: "custom-folder-name"
          # max-shard-chars: "450000"
          # max-single-file-bytes: "350000"
          # dry-run: "false"
```

---

#### Pattern B: Vendored Script (Self-Contained in Target Repo)

If you prefer not to reference an external action repository, copy `notebook-sync.py` and `notebook-sync.yml` directly into your target repository:

```text
your-repo/
└── .github/
    ├── scripts/
    │   └── notebook-sync.py
    └── workflows/
        └── notebook-sync.yml
```

1. **Create `.github/workflows/notebook-sync.yml` with Least-Privilege Permissions:**
   ```yaml
   name: Sync Repo to Gemini Notebook (Google Drive)

   on:
     push:
       branches:
         - main
         - master
     workflow_dispatch:

   # Least-privilege token scope: read-only access to repository contents
   permissions:
     contents: read

   concurrency:
     group: notebook-drive-sync-${{ github.repository }}
     cancel-in-progress: true

   jobs:
     sync-to-drive:
       runs-on: ubuntu-latest
       timeout-minutes: 15

       steps:
         - name: Checkout Repository
           uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
           with:
             fetch-depth: 1

         - name: Set up Python 3.12
           uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
           with:
             python-version: "3.12"

         - name: Run Bottom-Up Shard Bundler & Push to GAS Webhook
           env:
             GAS_WEBHOOK_URL: ${{ secrets.GAS_WEBHOOK_URL }}
             GAS_WEBHOOK_SECRET: ${{ secrets.GAS_WEBHOOK_SECRET }}
           run: |
             if [ -f ".github/scripts/notebook-sync.py" ]; then
               python3 .github/scripts/notebook-sync.py
             elif [ -f "notebook-sync.py" ]; then
               python3 notebook-sync.py
             else
               echo "ERROR: notebook-sync.py not found." >&2
               exit 1
             fi
   ```

2. **(Optional) Local Dry-Run Verification:**
   Before pushing, preview how `notebook-sync.py` will shard your repository locally without sending any network requests:
   ```bash
   DRY_RUN=true python3 .github/scripts/notebook-sync.py
   ```

3. **Commit and Push:**
   ```bash
   git add .github/scripts/notebook-sync.py .github/workflows/notebook-sync.yml
   git commit -m "ci: enable live Gemini Notebook Google Drive sync"
   git push
   ```

---

### Phase 4: Connect the Repo Subfolder to a Gemini Notebook

1. Wait ~15 seconds after `git push` for the GitHub Action to complete.
2. Open your Google Drive. You will find a newly created folder hierarchy:
   ```text
   My Drive/
   └── Gemini-Notebook-Mirrors/
       └── your-repo-name/
           ├── your-repo-name__00-ROOT          (Native Google Doc)
           ├── your-repo-name__01-src           (Native Google Doc)
           └── your-repo-name__01-docs          (Native Google Doc)
   ```
3. Open **Gemini Web UI -> Notebooks** (or [NotebookLM](https://notebooklm.google.com/)) and create a new Notebook for your project.
4. Click **Add Source** -> **Google Drive**, navigate into `Gemini-Notebook-Mirrors/your-repo-name/`, and select the native Google Docs inside that folder.

---

## Configuration Reference (`env:` Variables & `action.yml` Inputs)

| Environment Variable (Pattern B) | `action.yml` Input (Pattern A) | Default | Description |
|---|---|---|---|
| `GAS_WEBHOOK_URL` | `gas-webhook-url` | *(Required)* | Deployed Google Apps Script Web App `/exec` URL. |
| `GAS_WEBHOOK_SECRET` | `gas-webhook-secret` | *(Required)* | Shared secret matching `SECRET_TOKEN` in `Code.gs`. |
| `REPO_NAME` | `repo-name` | Auto-detected | Overrides the subfolder and document prefix name in Google Drive (defaults to the GitHub repository name). |
| `MAX_SHARD_CHARS` | `max-shard-chars` | `450000` | Maximum character ceiling per Google Doc shard (2.26x safety margin below Google Docs' 1.02M character limit, inclusive of header & manifest overhead). |
| `MAX_SINGLE_FILE_BYTES` | `max-single-file-bytes` | `350000` | Skips individual oversized text/data files exceeding this byte threshold (automatically clamped to `<= 85%` of `MAX_SHARD_CHARS`). |
| `DRY_RUN` | `dry-run` | `false` | Set to `true` to compute shards and print stats locally without calling the webhook. |

---

## Operational Runbook & Troubleshooting

* **`ReferenceError: Drive is not defined` (Status 500 from GAS):**
  You forgot to enable the **Drive API v3** Advanced Service in the Apps Script editor (see Phase 1, Step 2). Enable it, then click **Deploy -> Manage deployments -> Edit (pencil icon) -> Version: New version -> Deploy**.
* **`Refusing to drop POST payload on HTTP 302 redirect` or Non-JSON Response:**
  When deploying the Apps Script Web App, **Who has access** was set to `Only myself` instead of `Anyone`, or `GAS_WEBHOOK_URL` points to a `/dev` URL instead of `/exec`. Update the deployment access setting to `Anyone` and ensure the URL ends with `/exec`.
* **`Action: skipped_unchanged` or `Action: restored_from_stale` in GitHub Actions Logs:**
  This is expected behavior! `skipped_unchanged` means the composite Git blob SHA-1 hash for that shard matches the `git-shard-hash:<hash>` stored in the Google Doc's description metadata, saving Drive API quota and execution time. `restored_from_stale` means a previously `[STALE]` shard was re-activated with identical contents and had its `[STALE] ` prefix removed automatically.
* **Handling `[STALE]` Documents:**
  When you rename or delete a directory in your repository so that a previous shard is no longer generated, the Apps Script renames the orphaned Google Doc to `[STALE] <repo>__<old-shard>`. It does not auto-delete the file so your Notebook never suffers silent broken references. You can safely delete `[STALE]` docs from Google Drive and remove them from your Notebook whenever convenient.
* **Updating `Code.gs` After Initial Deployment:**
  In Google Apps Script, saving changes to `Code.gs` does **not** automatically update a live `/exec` Web App URL. After editing `Code.gs`, always click **Deploy -> Manage deployments -> Edit (pencil icon) -> Version: New version -> Deploy** (this keeps your `/exec` URL identical while activating the new code).

---

## License

Distributed under the MIT License. See [`LICENSE`](./LICENSE) for more information.