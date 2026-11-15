# Live GitHub-to-Gemini Notebook Sync

**Zero-daemon, automated GitHub repository synchronization into native Google Docs for live context inside Gemini Notebooks (NotebookLM).**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](#license)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Google Apps Script](https://img.shields.io/badge/Google%20Apps%20Script-V8-green.svg)](https://script.google.com/)

---

## The Problem

Keeping a codebase synchronized with a Gemini Web UI Notebook (or NotebookLM) normally runs into three major architectural roadblocks:

1. **Git Corruption via Desktop Sync Clients:** Pointing the Google Drive for Desktop client directly at a local Git repository causes file-locking collisions on `.git/index.lock` and loose objects, corrupting local commit graphs while uploading `node_modules/` bloat.
2. **The Native MIME-Type Gate:** Gemini Notebooks only auto-sync native Google Workspace documents (`application/vnd.google-apps.document`). Raw `.js`, `.ts`, `.py`, or `.md` files uploaded to Drive are either rejected by file-extension filters or ingested once as static, frozen snapshots that never auto-refresh.
3. **Single-Document Context Limits:** Dumping an entire repository into a single file hits Google Docs' 1.02M character ceiling and degrades Retrieval-Augmented Generation (RAG) precision.

## The Solution

This project bridges GitHub Actions directly to a lightweight, self-hosted **Google Apps Script Web App (`doPost`)** running under your own Google account—eliminating local background processes, GCP Service Account IAM complexity, and third-party servers.

```text
[VS Code: git push] 
       │
       ▼
[GitHub Actions: notebook-sync.py]
  ├─ Extracts Git index & native blob SHA-1s (`git ls-files -s`)
  ├─ Isolates root files into `00-ROOT`
  ├─ Compacts deep leaf folders bottom-up (<= 450k chars per shard)
  └─ Sends 1 HTTP POST per shard
       │
       ▼
[Google Apps Script Web App (`Code.gs`)]
  ├─ Verifies composite Git shard hash against Drive file metadata
  ├─ Skips unchanged shards in ~100ms
  └─ Overwrites changed native Google Docs in-place (preserving FILE_ID)
       │
       ▼
[Google Drive: Gemini-Notebook-Mirrors/<repo-name>/]
  ├─ <repo>__00-ROOT   (Native Google Doc)
  ├─ <repo>__01-src    (Native Google Doc)
  └─ <repo>__01-docs   (Native Google Doc)
       │
       ▼
[Gemini Web UI Notebook] -> Auto-syncs updated Docs & allows per-folder context toggling!
```

---

## Key Features

* **Preserved Google Drive `FILE_ID`s:** Updates overwrite the body of existing native Google Docs in-place via Drive API v3 (`Drive.Files.update`), so your Gemini Notebook never loses its source link.
* **Strict Root Isolation & Bottom-Up Leaf Compaction:**
  * Root-level files (`README.md`, `package.json`, configs) always stay isolated in their own `<repo>__00-ROOT` document.
  * Pass-through directories containing only subdirectories never create empty Google Docs.
  * Deep subdirectories automatically merge upward into their parent module until reaching the 450,000-character threshold (stopping at top-level directories like `src/`, `docs/`, or `tests/`), giving you clean checkboxes in the Gemini Notebook sidebar to toggle active context.
* **Git Blob SHA-1 Delta Skipping:** Computes a deterministic composite hash from Git's native index (`git ls-files -s`) and stores it in the Google Doc's `description` metadata. Unchanged shards are skipped without re-uploading document bodies.
* **Single Deployment, Unlimited Repos:** Deploy the Google Apps Script once. Every connected GitHub repository automatically provisions its own isolated subfolder (`Gemini-Notebook-Mirrors/<repo-name>/`).
* **Safe Refactoring (`[STALE]` Flagging):** If you rename or delete a folder in your repo, orphaned Google Docs are automatically prefixed with `[STALE]` rather than silently deleted.

---

## Repository Contents

* `Code.gs` (`gas_webhook_receiver.txt`) — The Google Apps Script Web App receiver.
* `notebook-sync.py` (`github_notebook_sync_py.txt`) — Zero-dependency Python 3 bundler and bottom-up directory sharding engine.
* `notebook-sync.yml` (`github_workflow_sync_yml.txt`) — Drop-in GitHub Actions workflow triggered on `push` or manual dispatch.

---

## Installation & Setup

### Step 1: Deploy the Google Apps Script Web App (One-Time Setup)

You only need to do this **once** for your Google account. All of your repositories can share this single webhook URL.

1. Go to [script.google.com](https://script.google.com) and click **New project**. Name it `Gemini Notebook Repo Sync`.
2. **Enable the Drive API v3 Advanced Service:**
   * In the left sidebar, click **Services (`+`)**.
   * Select **Drive API**, set Version to **`v3`**, leave Identifier as **`Drive`**, and click **Add**.
3. Copy the contents of `Code.gs` into the editor, replacing any existing code.
4. Generate a strong secret token (e.g., `openssl rand -hex 24`) and paste it into line 12 of `Code.gs`:
   ```javascript
   const SECRET_TOKEN = "your-generated-secret-token";
   ```
5. Click **Save** (`Ctrl+S`), then click **Deploy** -> **New deployment**:
   * **Select type:** `Web app`
   * **Description:** `v1 Production Receiver`
   * **Execute as:** `Me`
   * **Who has access:** `Anyone` *(Requests are authenticated by your `SECRET_TOKEN`)*
6. Click **Deploy**, authorize the script with your Google account (**Advanced** -> **Go to Gemini Notebook Repo Sync (unsafe)** -> **Allow**), and copy the generated **Web app URL** (`https://script.google.com/macros/s/.../exec`).

---

### Step 2: Add Secrets to GitHub

In the GitHub repository you want to sync (or at your GitHub Organization level to share across all repos):

1. Navigate to **Settings** -> **Secrets and variables** -> **Actions**.
2. Create two repository secrets:
   * `GAS_WEBHOOK_URL`: Paste the Web App URL from Step 1.
   * `GAS_WEBHOOK_SECRET`: Paste the `SECRET_TOKEN` you set in `Code.gs`.

---

### Step 3: Add the Sync Script & Workflow to Your Repo

Copy `notebook-sync.py` and `notebook-sync.yml` into your target repository:

```text
your-repo/
└── .github/
    ├── scripts/
    │   └── notebook-sync.py
    └── workflows/
        └── notebook-sync.yml
```

Commit and push to `main` (or `master`):

```bash
git add .github/scripts/notebook-sync.py .github/workflows/notebook-sync.yml
git commit -m "ci: enable live Gemini Notebook Google Drive sync"
git push
```

---

### Step 4: Link to a Gemini Notebook

1. Once the GitHub Action finishes (~15 seconds), open Google Drive and locate `My Drive/Gemini-Notebook-Mirrors/<your-repo-name>/`.
2. Open **Gemini Web UI -> Notebooks** (or [NotebookLM](https://notebooklm.google.com/)) and create a notebook for your repository.
3. Click **Add Source** -> **Google Drive**, open `Gemini-Notebook-Mirrors/<your-repo-name>/`, and select the generated Google Docs.

---

## Local Testing & Configuration

You can preview how `notebook-sync.py` will shard any local Git repository without sending network requests by running a dry run:

```bash
DRY_RUN=true python3 .github/scripts/notebook-sync.py
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GAS_WEBHOOK_URL` | *(Required)* | Deployed Google Apps Script Web App `/exec` URL. |
| `GAS_WEBHOOK_SECRET` | *(Required)* | Shared secret matching `SECRET_TOKEN` in `Code.gs`. |
| `REPO_NAME` | Auto-detected | Overrides the subfolder and document prefix name in Google Drive. |
| `MAX_SHARD_CHARS` | `450000` | Maximum character ceiling per Google Doc shard (safely below Google Docs' 1.02M char limit). |
| `MAX_SINGLE_FILE_BYTES` | `350000` | Skips individual oversized text/data files exceeding this byte threshold. |
| `DRY_RUN` | `false` | Set to `true` to compute shards and print stats locally without calling the webhook. |

---

## License

Distributed under the MIT License. See [`LICENSE`](./LICENSE) for more information.
