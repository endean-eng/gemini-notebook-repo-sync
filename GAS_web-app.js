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
  // Wait up to 30 seconds for concurrent shard requests to finish folder creation
  lock.waitLock(30000);

  try {
    if (!e || !e.postData || !e.postData.contents) {
      return jsonResponse_({ status: 400, error: "Empty request body" });
    }

    const payload = JSON.parse(e.postData.contents);
    if (!payload.secret || payload.secret !== SECRET_TOKEN) {
      return jsonResponse_({ status: 403, error: "Unauthorized: Invalid secret token" });
    }

    const repo = sanitizeName_(payload.repo || "unknown-repo");
    const shardName = sanitizeName_(payload.shardName || "00-ROOT");
    const content = payload.content || "";
    const shardHash = payload.shardHash || "";
    const activeShards = payload.activeShards || null;

    // 1. Ensure Master Folder and Repo Subfolder exist
    const masterFolderId = findOrCreateFolder_("root", MASTER_FOLDER_NAME);
    const repoFolderId = findOrCreateFolder_(masterFolderId, repo);

    // 2. Check if the Native Google Doc for this shard already exists
    const docTitle = `${repo}__${shardName}`;
    const existingDoc = findDocInFolder_(repoFolderId, docTitle);

    // 3. Fast-Path Delta Skip: If the existing doc's description already holds the
    //    exact same composite Git tree/blob hash, skip the Drive media upload!
    if (existingDoc && shardHash && existingDoc.description === `git-shard-hash:${shardHash}`) {
      if (Array.isArray(activeShards)) {
        flagStaleShards_(repoFolderId, repo, activeShards);
      }
      return jsonResponse_({
        status: 200,
        action: "skipped_unchanged",
        repo: repo,
        shardName: shardName,
        fileId: existingDoc.id,
        chars: content.length
      });
    }

    // 4. Create or Overwrite the Native Google Doc in place (preserves FILE_ID)
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

    // 5. On the final shard push, prefix any orphaned/deleted shards with [STALE]
    if (Array.isArray(activeShards)) {
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
    lock.releaseLock();
  }
}

/**
 * Finds or creates a subfolder inside a given parent folder ID.
 */
function findOrCreateFolder_(parentId, folderName) {
  const escapedName = folderName.replace(/'/g, "\\'");
  const query = `'${parentId}' in parents and name = '${escapedName}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false`;
  const res = Drive.Files.list({
    q: query,
    fields: "files(id, name)",
    pageSize: 1
  });

  if (res.files && res.files.length > 0) {
    return res.files[0].id;
  }

  const created = Drive.Files.create({
    name: folderName,
    parents: [parentId],
    mimeType: "application/vnd.google-apps.folder"
  });
  return created.id;
}

/**
 * Finds a native Google Doc inside a folder (also matches if previously marked [STALE]).
 */
function findDocInFolder_(folderId, docTitle) {
  const escapedTitle = docTitle.replace(/'/g, "\\'");
  const staleTitle = `[STALE] ${docTitle}`.replace(/'/g, "\\'");
  const query = `'${folderId}' in parents and (name = '${escapedTitle}' or name = '${staleTitle}') and mimeType = 'application/vnd.google-apps.document' and trashed = false`;
  const res = Drive.Files.list({
    q: query,
    fields: "files(id, name, description)",
    pageSize: 1
  });

  if (res.files && res.files.length > 0) {
    return res.files[0];
  }
  return null;
}

/**
 * Marks any Google Docs in the repo folder that are no longer part of activeShards as [STALE].
 * Preserves the file rather than deleting it so Notebook links don't silently break without notice.
 */
function flagStaleShards_(folderId, repo, activeShards) {
  const validTitles = new Set(activeShards.map(s => `${repo}__${sanitizeName_(s)}`));
  const query = `'${folderId}' in parents and mimeType = 'application/vnd.google-apps.document' and trashed = false`;
  const res = Drive.Files.list({
    q: query,
    fields: "files(id, name)",
    pageSize: 100
  });

  if (!res.files) return;
  for (const file of res.files) {
    if (!validTitles.has(file.name) && !file.name.startsWith("[STALE] ")) {
      Drive.Files.update({ name: `[STALE] ${file.name}` }, file.id);
    }
  }
}

function sanitizeName_(str) {
  return String(str).replace(/[\\/:*?"<>|]/g, "-").trim();
}

function jsonResponse_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
