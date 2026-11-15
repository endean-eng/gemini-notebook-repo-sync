/**
 * ============================================================================
 * GEMINI NOTEBOOK LIVE REPO MIRROR — GOOGLE APPS SCRIPT WEB APP (Code.gs)
 * ============================================================================
 * PREREQUISITE:
 * In the left sidebar of the Apps Script Editor, click "Services (+)"
 * -> Select "Drive API" -> Version "v3" -> Click "Add".
 * ============================================================================
 */

// 1. Replace with a strong random string (matches your GAS_WEBHOOK_SECRET in GitHub)
const SECRET_TOKEN = "REPLACE_WITH_YOUR_SECRET_TOKEN";

// 2. Master folder in your Google Drive root that will house all per-repo subfolders
const MASTER_FOLDER_NAME = "Gemini-Notebook-Mirrors";

/**
 * Handles incoming POST requests (1 POST per compacted shard from GitHub Actions).
 */
function doPost(e) {
  const lock = LockService.getScriptLock();
  let hasLock = false;

  try {
    // Acquire concurrency lock inside try/catch so timeouts return JSON 503 instead of HTML
    hasLock = lock.tryLock(30000);
    if (!hasLock) {
      return jsonResponse_({
        status: 503,
        error: "Server busy: Could not acquire script lock within 30s"
      });
    }

    if (!SECRET_TOKEN || SECRET_TOKEN === "REPLACE_WITH_YOUR_SECRET_TOKEN") {
      return jsonResponse_({
        status: 500,
        error: "Server misconfigured: Default SECRET_TOKEN placeholder has not been replaced in Code.gs"
      });
    }

    if (!e || !e.postData || !e.postData.contents) {
      return jsonResponse_({ status: 400, error: "Empty request body" });
    }

    const payload = JSON.parse(e.postData.contents);
    if (!timingSafeEqual_(payload.secret, SECRET_TOKEN)) {
      return jsonResponse_({ status: 403, error: "Unauthorized: Invalid secret token" });
    }

    const masterFolderName = sanitizeName_(MASTER_FOLDER_NAME) || "Gemini-Notebook-Mirrors";
    const repo = sanitizeName_(payload.repo || "") || "unknown-repo";
    const shardName = sanitizeName_(payload.shardName || "") || "00-ROOT";
    const content = payload.content || "";
    const shardHash = payload.shardHash || "";
    const activeShards = Array.isArray(payload.activeShards) ? payload.activeShards : null;
    const syncStaleOnly = Boolean(payload.syncStaleOnly);

    // 1. Ensure Master Folder and Repo Subfolder exist
    const masterFolderId = findOrCreateFolder_("root", masterFolderName);
    const repoFolderId = findOrCreateFolder_(masterFolderId, repo);

    // 2. Empty-Repo Sweep Mode: If all eligible files were deleted from the repo,
    //    mark all existing Google Docs in the repo folder as [STALE] and return.
    if (syncStaleOnly) {
      flagStaleShards_(repoFolderId, repo, activeShards || []);
      return jsonResponse_({
        status: 200,
        action: "stale_sweep_only",
        repo: repo,
        shardName: shardName,
        fileId: null,
        chars: 0
      });
    }

    // 3. Check if the Native Google Doc for this shard already exists
    //    (queries active title first; falls back to [STALE] title only if no active doc exists)
    const docTitle = `${repo}__${shardName}`;
    const existingDoc = findDocInFolder_(repoFolderId, docTitle);

    // 4. Fast-Path Delta Skip: If the existing doc's description already holds the
    //    exact same composite Git tree/blob hash, skip the Drive media upload!
    if (existingDoc && shardHash && existingDoc.description === `git-shard-hash:${shardHash}`) {
      let skipAction = "skipped_unchanged";
      // If this shard was previously marked [STALE] (or differed in casing) and has now
      // been restored with identical contents, update its title to the active docTitle.
      if (existingDoc.name !== docTitle) {
        Drive.Files.update({ name: docTitle }, existingDoc.id);
        skipAction = "restored_from_stale";
      }
      if (activeShards !== null) {
        flagStaleShards_(repoFolderId, repo, activeShards);
      }
      return jsonResponse_({
        status: 200,
        action: skipAction,
        repo: repo,
        shardName: shardName,
        fileId: existingDoc.id,
        chars: content.length
      });
    }

    // 5. Create or Overwrite the Native Google Doc in place (preserves FILE_ID)
    const mediaBlob = Utilities.newBlob(content, "text/plain", `${docTitle}.txt`);
    const fileMetadata = {
      name: docTitle,
      mimeType: "application/vnd.google-apps.document",
      description: shardHash ? `git-shard-hash:${shardHash}` : "Synced from GitHub"
    };

    let fileId;
    let action;
    if (existingDoc) {
      const updated = Drive.Files.update(fileMetadata, existingDoc.id, mediaBlob);
      fileId = updated.id;
      action = "updated";
    } else {
      fileMetadata.parents = [repoFolderId];
      const created = Drive.Files.create(fileMetadata, mediaBlob);
      fileId = created.id;
      action = "created";
    }

    // 6. On the final shard push, prefix any orphaned/deleted shards with [STALE]
    if (activeShards !== null) {
      flagStaleShards_(repoFolderId, repo, activeShards);
    }

    return jsonResponse_({
      status: 200,
      action: action,
      repo: repo,
      shardName: shardName,
      fileId: fileId,
      chars: content.length
    });

  } catch (err) {
    return jsonResponse_({
      status: 500,
      error: err.toString(),
      stack: err.stack || ""
    });
  } finally {
    if (hasLock) {
      lock.releaseLock();
    }
  }
}

/**
 * Constant-time string comparison to mitigate timing attacks on SECRET_TOKEN.
 */
function timingSafeEqual_(a, b) {
  if (typeof a !== "string" || typeof b !== "string") {
    return false;
  }
  const lenA = a.length;
  const lenB = b.length;
  const maxLen = Math.max(lenA, lenB);
  let result = lenA ^ lenB;
  for (let i = 0; i < maxLen; i++) {
    const charA = i < lenA ? a.charCodeAt(i) : 0;
    const charB = i < lenB ? b.charCodeAt(i) : 0;
    result |= (charA ^ charB);
  }
  return result === 0;
}

/**
 * Escapes backslashes and single quotes for Google Drive API v3 query strings.
 */
function escapeDriveQueryLiteral_(str) {
  return String(str).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}

/**
 * Finds or creates a subfolder inside a given parent folder ID.
 * Prefers exact case-sensitive matches when multiple candidates are returned by Drive.
 */
function findOrCreateFolder_(parentId, folderName) {
  const escapedParent = escapeDriveQueryLiteral_(parentId);
  const escapedName = escapeDriveQueryLiteral_(folderName);
  const query = `'${escapedParent}' in parents and name = '${escapedName}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false`;
  const res = Drive.Files.list({
    q: query,
    fields: "files(id, name)",
    pageSize: 20
  });

  if (res.files && res.files.length > 0) {
    const exactMatch = res.files.find(f => f.name === folderName);
    return exactMatch ? exactMatch.id : res.files[0].id;
  }

  const created = Drive.Files.create({
    name: folderName,
    parents: [parentId],
    mimeType: "application/vnd.google-apps.folder"
  });
  return created.id;
}

/**
 * Two-pass deterministic lookup for a native Google Doc inside a folder:
 * Pass 1 searches strictly for the active `docTitle` (preferring exact case-sensitive match).
 * Pass 2 falls back to `[STALE] ${docTitle}` only if no active document exists, preventing
 * ambiguity when both an active and a stale copy exist in the folder.
 */
function findDocInFolder_(folderId, docTitle) {
  const escapedFolder = escapeDriveQueryLiteral_(folderId);
  const escapedTitle = escapeDriveQueryLiteral_(docTitle);

  // Pass 1: Look for active document
  const activeQuery = `'${escapedFolder}' in parents and name = '${escapedTitle}' and mimeType = 'application/vnd.google-apps.document' and trashed = false`;
  const activeRes = Drive.Files.list({
    q: activeQuery,
    fields: "files(id, name, description)",
    pageSize: 20
  });

  if (activeRes.files && activeRes.files.length > 0) {
    const exactActive = activeRes.files.find(f => f.name === docTitle);
    return exactActive || activeRes.files[0];
  }

  // Pass 2: Fallback to [STALE] document only if no active doc exists
  const staleName = `[STALE] ${docTitle}`;
  const escapedStale = escapeDriveQueryLiteral_(staleName);
  const staleQuery = `'${escapedFolder}' in parents and name = '${escapedStale}' and mimeType = 'application/vnd.google-apps.document' and trashed = false`;
  const staleRes = Drive.Files.list({
    q: staleQuery,
    fields: "files(id, name, description)",
    pageSize: 20
  });

  if (staleRes.files && staleRes.files.length > 0) {
    const exactStale = staleRes.files.find(f => f.name === staleName);
    return exactStale || staleRes.files[0];
  }

  return null;
}

/**
 * Marks any Google Docs in the repo folder that are no longer part of activeShards as [STALE].
 * Compares names case-insensitively and paginates across all files in the folder.
 */
function flagStaleShards_(folderId, repo, activeShards) {
  const validTitlesLower = new Set(
    activeShards.map(s => `${repo}__${sanitizeName_(s) || "00-ROOT"}`.toLowerCase())
  );
  const escapedFolder = escapeDriveQueryLiteral_(folderId);
  const query = `'${escapedFolder}' in parents and mimeType = 'application/vnd.google-apps.document' and trashed = false`;

  let pageToken = null;
  do {
    const res = Drive.Files.list({
      q: query,
      fields: "nextPageToken, files(id, name)",
      pageSize: 1000,
      pageToken: pageToken
    });

    if (res.files) {
      for (const file of res.files) {
        const nameLower = String(file.name || "").toLowerCase();
        if (!validTitlesLower.has(nameLower) && !file.name.startsWith("[STALE] ")) {
          Drive.Files.update({ name: `[STALE] ${file.name}` }, file.id);
        }
      }
    }
    pageToken = res.nextPageToken || null;
  } while (pageToken);
}

function sanitizeName_(str) {
  return String(str)
    .replace(/[\\/:*?"<>|\x00-\x1f]/g, "-")
    .replace(/^-+|-+$/g, "")
    .trim();
}

function jsonResponse_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}