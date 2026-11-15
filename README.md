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
  * Root-level files (`README.md`, `package.json`, configs) always stay isolated in their own `<repo>__00-ROOT` document, even if the entire repository is well under the character limit.
  * Pass-through directories containing only subdirectories (0 direct files) are pruned automatically and never create empty Google Docs.
  * Deep subdirectories automatically merge upward into their parent module until reaching the 450,000-character threshold (stopping at top-level directories like `src/`, `docs/`, or `tests/`), giving you clean checkboxes in the Gemini Notebook sidebar to toggle active context.
* **Git Blob SHA-1 Delta Skipping:** Computes a deterministic composite hash from Git's native index (`git ls-files -s`) and stores it in the Google Doc's `description` metadata. Unchanged shards are skipped in ~100ms without re-uploading document bodies.
* **Single Deployment, Unlimited Repos:** Deploy the Google Apps Script once. Every connected GitHub repository automatically provisions its own isolated subfolder (`Gemini-Notebook-Mirrors/<repo-name>/`).
* **Safe Refactoring (`[STALE]` Flagging):** If you rename or delete a folder in your repo, orphaned Google Docs are automatically prefixed with `[STALE]` rather than silently deleted, preventing broken Notebook links.

---

## Repository Contents

* `Code.gs` (`gas_webhook_receiver.txt`) — The Google Apps Script Web App receiver.
* `notebook-sync.py` (`github_notebook_sync_py.txt`) — Zero-dependency Python 3 bundler and bottom-up directory sharding engine.
* `notebook-sync.yml` (`github_workflow_sync_yml.txt`) — Drop-in GitHub Actions workflow triggered on `push` or manual dispatch.

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
   * Copy the contents of `Code.gs` (`gas_webhook_receiver.txt`) and paste them into the editor (replacing the default `myFunction`).
   * Generate a random secret token (for example, run `openssl rand -hex 24` in any terminal, or type a strong 32+ character passphrase).
   * Replace `"REPLACE_WITH_YOUR_SECRET_TOKEN"` on line 12 of `Code.gs` with your secret token:
     ```javascript
     const SECRET_TOKEN = "your-generated-secret-token";
     ```
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

### Phase 3: Add the Bundler Script & Workflow to a Repository

In your target repository, place the Python script and GitHub Actions workflow in `.github/`:

```text
your-repo/
└── .github/
    ├── scripts/
    │   └── notebook-sync.py
    └── workflows/
        └── notebook-sync.yml
```

1. **(Optional) Local Dry-Run Verification:**
   Before pushing, you can preview how `notebook-sync.py` will shard your repository locally without sending any network requests:
   ```bash
   DRY_RUN=true python3 .github/scripts/notebook-sync.py
   ```
   You will see console output listing `00-ROOT` (isolated root files) and each `01-<folder>` shard along with file counts, character counts, and composite Git blob hashes.

2. **Commit and Push:**
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

## Configuration & Environment Variables

You can customize the sharding behavior in `.github/workflows/notebook-sync.yml` via environment variables:

| Variable | Default | Description |
|---|---|---|
| `GAS_WEBHOOK_URL` | *(Required)* | Deployed Google Apps Script Web App `/exec` URL. |
| `GAS_WEBHOOK_SECRET` | *(Required)* | Shared secret matching `SECRET_TOKEN` in `Code.gs`. |
| `REPO_NAME` | Auto-detected | Overrides the subfolder and document prefix name in Google Drive (defaults to the GitHub repository name). |
| `MAX_SHARD_CHARS` | `450000` | Maximum character ceiling per Google Doc shard (safely below Google Docs' 1.02M character limit and Gemini Notebook's 500k-word limit). |
| `MAX_SINGLE_FILE_BYTES` | `350000` | Skips individual oversized text/data files exceeding this byte threshold. |
| `DRY_RUN` | `false` | Set to `true` to compute shards and print stats locally without calling the webhook. |

---

## Operational Runbook & Troubleshooting

* **`ReferenceError: Drive is not defined` (Status 500 from GAS):**
  You forgot to enable the **Drive API v3** Advanced Service in the Apps Script editor (see Phase 1, Step 2). Enable it, then click **Deploy -> Manage deployments -> Edit (pencil icon) -> Version: New version -> Deploy**.
* **`HTTPError: HTTP Error 401: Unauthorized` or HTML Login Page Response:**
  When deploying the Apps Script Web App, **Who has access** was set to `Only myself` instead of `Anyone`. Update the deployment access setting to `Anyone`.
* **`Action: skipped_unchanged` in GitHub Actions Logs:**
  This is expected behavior! It means the composite Git blob SHA-1 hash for that shard matches the `git-shard-hash:<hash>` stored in the Google Doc's description metadata, saving Drive API quota and execution time.
* **Handling `[STALE]` Documents:**
  When you rename or delete a directory in your repository so that a previous shard is no longer generated, the Apps Script renames the orphaned Google Doc to `[STALE] <repo>__<old-shard>`. It does not auto-delete the file so your Notebook never suffers silent broken references. You can safely delete `[STALE]` docs from Google Drive and remove them from your Notebook whenever convenient.
* **Updating `Code.gs` After Initial Deployment:**
  In Google Apps Script, saving changes to `Code.gs` does **not** automatically update a live `/exec` Web App URL. After editing `Code.gs`, always click **Deploy -> Manage deployments -> Edit (pencil icon) -> Version: New version -> Deploy** (this keeps your `/exec` URL identical while activating the new code).

---

## License

Distributed under the MIT License. See [`LICENSE`](./LICENSE) for more information.
