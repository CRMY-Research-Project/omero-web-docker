"""Data-access governance store for the download gate (WS-I).

State lives in a single **SQLite** database (WAL mode) on the writable
``DOWNLOAD_GATE_DIR`` volume - no OMERO.web Django-DB configuration is
required, so the gate stays self-contained on its own volume.

Why SQLite (and not the old per-request JSON files): the Phase-1 store
kept one JSON document per request and ``has_approval`` scanned *every*
request file on *every* call - an O(n) directory walk invoked on each
``download_status``. SQLite gives indexed lookups, transactions and safe
concurrent reads (WAL = many readers + one writer) at 50+ users without
any external service.

The module models four entities:

* **requests**  - a user's ask for download access (+ typed documents).
* **grants**    - what an approver actually granted: a scope
  (image/dataset/project) + expiry, split out from the request (I3).
* **policies**  - per-dataset approver + required-docs configuration (I2).
* **audit**     - an append-only log of governance actions (I5).

Layout under ``DOWNLOAD_GATE_DIR`` (default /opt/omero/web/download_gate):

    gate.db                      SQLite database (+ gate.db-wal / -shm)
    docs/<request_id>/<file>     supporting documents for that request
    requests/<request_id>.json   legacy Phase-1 state (auto-imported once)

Purity / testability: this module must stay importable *without*
``omero.gateway`` (no BlitzGateway import here) so it unit-tests offline.
All OMERO lookups (image->dataset->project lineage, group membership,
approver resolution against live users) belong to the view layer, which
feeds resolved ids/principals into these pure functions.

Postgres upgrade path: every caller goes through this facade, so moving
to Postgres later is a drop-in - replace ``_connect``/the SQL dialect
(or back the same public functions with Django ORM models
``DownloadRequest`` / ``AccessGrant`` / ``DatasetPolicy`` / ``AuditEvent``)
without touching ``views.py``. Keep the public function signatures below
stable and the swap is invisible to the rest of the plugin.
"""

import json
import os
import posixpath
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

# --- request status -------------------------------------------------------
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
VALID_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED)

# --- request targets ------------------------------------------------------
TARGET_IMAGE = "image"
TARGET_DATASET = "dataset"
VALID_TARGETS = (TARGET_IMAGE, TARGET_DATASET)

# --- grant scopes (I3): approver picks how wide the grant reaches ----------
SCOPE_IMAGE = "image"
SCOPE_DATASET = "dataset"
SCOPE_PROJECT = "project"
VALID_SCOPES = (SCOPE_IMAGE, SCOPE_DATASET, SCOPE_PROJECT)

# --- typed supporting documents (I4) --------------------------------------
DOC_ETHICS = "ethics"
DOC_PROPOSAL = "proposal"
DOC_DUA = "dua"
VALID_DOC_TYPES = (DOC_ETHICS, DOC_PROPOSAL, DOC_DUA)

# --- audit actions (I5) ---------------------------------------------------
ACTION_REQUEST = "request"
ACTION_APPROVE = "approve"
ACTION_DENY = "deny"
ACTION_REVOKE = "revoke"
ACTION_DOWNLOAD = "download"
ACTION_POLICY_SET = "policy_set"
VALID_ACTIONS = (ACTION_REQUEST, ACTION_APPROVE, ACTION_DENY, ACTION_REVOKE,
                 ACTION_DOWNLOAD, ACTION_POLICY_SET)

_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_UUID_RE = re.compile(r"[0-9a-f]{32}")

# db paths that have had schema-ensure + legacy-migration run this process.
_INITIALIZED = set()


class MissingDocumentError(ValueError):
    """Raised when a request omits documents its dataset policy requires."""

    def __init__(self, missing):
        self.missing = list(missing)
        super().__init__(
            "missing required document(s): %s" % ", ".join(self.missing))


# --------------------------------------------------------------------------
# Paths / small helpers (kept from Phase-1 so callers & tests don't change)
# --------------------------------------------------------------------------
def base_dir():
    return os.environ.get("DOWNLOAD_GATE_DIR",
                          "/opt/omero/web/download_gate")


def _db_path():
    root = base_dir()
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, "gate.db")


def _requests_dir():
    # only used for legacy Phase-1 JSON import now
    path = os.path.join(base_dir(), "requests")
    os.makedirs(path, exist_ok=True)
    return path


def docs_dir(request_id):
    path = os.path.join(base_dir(), "docs", request_id)
    os.makedirs(path, exist_ok=True)
    return path


def safe_filename(name):
    """Reduce an uploaded filename to a safe basename."""
    name = os.path.basename(name or "")
    name = _UNSAFE_FILENAME_RE.sub("_", name)
    return name[:128] or "unnamed"


def xaccel_internal_uri(managed_root, internal_prefix, file_path, file_name):
    """Map an OMERO original file to an NGINX X-Accel-Redirect internal URI
    (WS-H / H2), or ``None`` if it would not resolve strictly inside the
    served root.

    ``managed_root`` is the managed-repository root OMERO reports paths
    relative to (e.g. ``/OMERO/ManagedRepository``); ``file_path`` /
    ``file_name`` are ``OriginalFile.getPath()`` / ``getName()``.
    ``internal_prefix`` is the NGINX ``internal`` location (e.g.
    ``/_protected``) whose ``alias`` points at that same managed-repo root.

    Returns ``<internal_prefix>/<url-encoded relative path>``. Returns
    ``None`` when the resolved path escapes ``managed_root`` (traversal via
    ``..`` or an absolute path/name) - the security guard, so a crafted
    stored path can never reach an arbitrary file. POSIX path semantics are
    used deliberately: files are served from the Linux container regardless
    of the caller's OS, so tests are stable cross-platform.
    """
    root = posixpath.normpath(managed_root or "")
    if not root or root in (".", "/"):
        return None
    prefix = (internal_prefix or "").strip("/")
    if not prefix:
        return None
    candidate = posixpath.normpath(
        posixpath.join(root, file_path or "", file_name or ""))
    # must be strictly *inside* the root (not the root dir itself)
    if candidate == root or not candidate.startswith(root + "/"):
        return None
    rel = candidate[len(root) + 1:]
    if not rel:
        return None
    return "/" + prefix + "/" + quote(rel)


def _now():
    return datetime.now(timezone.utc)


def _valid_request_id(request_id):
    return bool(request_id) and _UUID_RE.fullmatch(request_id) is not None


def _request_path(request_id):
    """Legacy JSON path for a request id, or None if the id is unsafe.

    Retained as the uuid-format guard (a crafted id can never escape the
    docs/requests directories) even though live state is now in SQLite.
    """
    if not _valid_request_id(request_id or ""):
        return None
    return os.path.join(_requests_dir(), "%s.json" % request_id)


def _normalize_ts(value):
    """Coerce a datetime / ISO string / ``YYYY-MM-DD`` to an ISO string.

    Naive values are assumed UTC. Returns None for empty input.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid timestamp: %r" % value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def compute_expiry(expires_days=None, expires_at=None):
    """Resolve an approver's expiry choice to an ISO string (or None).

    ``expires_at`` (explicit date/datetime) wins over ``expires_days``
    (a duration); both unset means a standing grant (never expires).
    """
    if expires_at not in (None, ""):
        return _normalize_ts(expires_at)
    if expires_days:
        return (_now() + timedelta(days=int(expires_days))).isoformat()
    return None


# --------------------------------------------------------------------------
# Connection + schema
# --------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id           TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    user_id      INTEGER,
    target_type  TEXT NOT NULL,
    target_id    INTEGER NOT NULL,
    reason       TEXT,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    reviewed_by  TEXT,
    reviewed_at  TEXT,
    review_note  TEXT,
    expires_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_requests_username ON requests(username);
CREATE INDEX IF NOT EXISTS ix_requests_status   ON requests(status);
CREATE INDEX IF NOT EXISTS ix_requests_target   ON requests(target_type, target_id);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL,
    filename    TEXT NOT NULL,
    doc_type    TEXT
);
CREATE INDEX IF NOT EXISTS ix_documents_request ON documents(request_id);

CREATE TABLE IF NOT EXISTS grants (
    id          TEXT PRIMARY KEY,
    principal   TEXT NOT NULL,
    scope_type  TEXT NOT NULL,
    scope_id    INTEGER NOT NULL,
    granted_by  TEXT,
    granted_at  TEXT NOT NULL,
    expires_at  TEXT,
    reason      TEXT,
    request_id  TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0,
    revoked_by  TEXT,
    revoked_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_grants_principal ON grants(principal);
CREATE INDEX IF NOT EXISTS ix_grants_scope     ON grants(scope_type, scope_id);

CREATE TABLE IF NOT EXISTS policies (
    dataset_id          INTEGER PRIMARY KEY,
    approver_principals TEXT,
    required_docs       TEXT,
    default_expiry_days INTEGER,
    auto_expire         INTEGER NOT NULL DEFAULT 1,
    updated_by          TEXT,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    detail      TEXT,
    dataset_id  INTEGER,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_ts      ON audit(ts);
CREATE INDEX IF NOT EXISTS ix_audit_action  ON audit(action);
CREATE INDEX IF NOT EXISTS ix_audit_dataset ON audit(dataset_id);
"""


def _connect():
    """Open the gate DB, ensuring WAL mode, schema and legacy import.

    A fresh connection per call keeps state resolvable from the current
    ``DOWNLOAD_GATE_DIR`` (so tests can point it at a tmp path and stay
    isolated) - WAL is a file-level setting so it persists across them.
    """
    path = _db_path()
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    key = os.path.abspath(path)
    if key not in _INITIALIZED:
        conn.executescript(_SCHEMA)
        conn.commit()
        _migrate_legacy_json(conn)
        _INITIALIZED.add(key)
    return conn


def _migrate_legacy_json(conn):
    """Import Phase-1 ``requests/*.json`` on first init if the DB is empty.

    Existing gate state (requests + approvals) is not lost when a site
    upgrades from the JSON store to SQLite. Approved legacy requests also
    seed an equivalent :func:`create_grant` so downloads keep working.
    """
    row = conn.execute("SELECT COUNT(*) AS n FROM requests").fetchone()
    if row["n"] > 0:
        return
    legacy_dir = os.path.join(base_dir(), "requests")
    if not os.path.isdir(legacy_dir):
        return
    for entry in sorted(os.listdir(legacy_dir)):
        if not entry.endswith(".json"):
            continue
        try:
            with open(os.path.join(legacy_dir, entry), "r",
                      encoding="utf-8") as f:
                req = json.load(f)
        except (OSError, ValueError):
            continue
        rid = req.get("id") or entry[:-len(".json")]
        if not _valid_request_id(rid):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO requests (id, username, user_id, "
            "target_type, target_id, reason, status, created_at, "
            "reviewed_by, reviewed_at, review_note, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, req.get("username"), req.get("user_id"),
             req.get("target_type"), int(req.get("target_id", 0)),
             req.get("reason", ""), req.get("status", STATUS_PENDING),
             req.get("created_at") or _now().isoformat(),
             req.get("reviewed_by"), req.get("reviewed_at"),
             req.get("review_note", ""), req.get("expires_at")))
        for fname in req.get("documents", []) or []:
            conn.execute(
                "INSERT INTO documents (request_id, filename, doc_type) "
                "VALUES (?,?,?)", (rid, fname, None))
        # An approved legacy request had request-shaped scope; recreate it
        # as a grant so has_approval (now grant-based) still covers it.
        if req.get("status") == STATUS_APPROVED:
            _insert_grant(
                conn,
                principal=req.get("username"),
                scope_type=req.get("target_type"),
                scope_id=int(req.get("target_id", 0)),
                granted_by=req.get("reviewed_by"),
                expires_at=req.get("expires_at"),
                reason=req.get("review_note", ""),
                request_id=rid)
    conn.commit()


# --------------------------------------------------------------------------
# Row -> dict adapters (stable public shapes)
# --------------------------------------------------------------------------
def _docs_for(conn, request_ids):
    if not request_ids:
        return {}
    marks = ",".join("?" * len(request_ids))
    rows = conn.execute(
        "SELECT request_id, filename, doc_type FROM documents "
        "WHERE request_id IN (%s) ORDER BY id" % marks,
        tuple(request_ids)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["request_id"], []).append(
            {"filename": r["filename"], "type": r["doc_type"]})
    return out


def _request_dict(row, docs):
    detail = docs or []
    return {
        "id": row["id"],
        "username": row["username"],
        "user_id": row["user_id"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "reason": row["reason"] or "",
        "documents": [d["filename"] for d in detail],
        "documents_detail": detail,
        "status": row["status"],
        "created_at": row["created_at"],
        "reviewed_by": row["reviewed_by"],
        "reviewed_at": row["reviewed_at"],
        "review_note": row["review_note"] or "",
        "expires_at": row["expires_at"],
    }


def _grant_dict(row):
    return {
        "id": row["id"],
        "principal": row["principal"],
        "scope_type": row["scope_type"],
        "scope_id": row["scope_id"],
        "granted_by": row["granted_by"],
        "granted_at": row["granted_at"],
        "expires_at": row["expires_at"],
        "reason": row["reason"] or "",
        "request_id": row["request_id"],
        "revoked": bool(row["revoked"]),
        "revoked_by": row["revoked_by"],
        "revoked_at": row["revoked_at"],
        "active": _grant_active(row),
    }


def _policy_dict(row):
    return {
        "dataset_id": row["dataset_id"],
        "approver_principals": json.loads(row["approver_principals"] or "[]"),
        "required_docs": json.loads(row["required_docs"] or "[]"),
        "default_expiry_days": row["default_expiry_days"],
        "auto_expire": bool(row["auto_expire"]),
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
    }


def _audit_dict(row):
    return {
        "id": row["id"],
        "actor": row["actor"],
        "action": row["action"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "detail": row["detail"] or "",
        "dataset_id": row["dataset_id"],
        "ts": row["ts"],
    }


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------
def get_request(request_id):
    if not _valid_request_id(request_id or ""):
        return None
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM requests WHERE id=?",
                           (request_id,)).fetchone()
        if row is None:
            return None
        docs = _docs_for(conn, [request_id])
        return _request_dict(row, docs.get(request_id, []))


def list_requests(status=None, username=None):
    """All requests, newest first, optionally filtered."""
    clauses, params = [], []
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    if username is not None:
        clauses.append("username=?")
        params.append(username)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM requests%s ORDER BY created_at DESC, rowid DESC"
            % where, tuple(params)).fetchall()
        docs = _docs_for(conn, [r["id"] for r in rows])
        return [_request_dict(r, docs.get(r["id"], [])) for r in rows]


def create_request(username, user_id, target_type, target_id, reason,
                   documents, required_docs=None):
    """Create a new pending request.

    ``documents`` is an iterable of ``(filename, chunk_iterable)`` or
    ``(filename, chunk_iterable, doc_type)`` tuples; they are written
    under docs/<request_id>/. When ``required_docs`` is given (from the
    dataset policy) and a required type is absent, raises
    :class:`MissingDocumentError` *before* any file is written.
    """
    if target_type not in VALID_TARGETS:
        raise ValueError("invalid target_type: %r" % target_type)

    items = list(documents)
    provided_types = set()
    for it in items:
        if len(it) >= 3 and it[2]:
            provided_types.add(it[2])
    if required_docs:
        missing = missing_required_docs(required_docs, provided_types)
        if missing:
            raise MissingDocumentError(missing)

    request_id = uuid.uuid4().hex
    saved = []  # (filename, doc_type)
    for it in items:
        filename = safe_filename(it[0])
        doc_type = it[2] if len(it) >= 3 else None
        chunks = it[1]
        while any(filename == s[0] for s in saved):
            filename = "_" + filename
        dest = os.path.join(docs_dir(request_id), filename)
        with open(dest, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
        saved.append((filename, doc_type))

    created = _now().isoformat()
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO requests (id, username, user_id, target_type, "
            "target_id, reason, status, created_at, reviewed_by, "
            "reviewed_at, review_note, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, username, user_id, target_type, int(target_id),
             reason or "", STATUS_PENDING, created, None, None, "", None))
        for filename, doc_type in saved:
            conn.execute(
                "INSERT INTO documents (request_id, filename, doc_type) "
                "VALUES (?,?,?)", (request_id, filename, doc_type))
        _insert_audit(
            conn, actor=username, action=ACTION_REQUEST,
            target_type="request", target_id=request_id,
            detail="target=%s:%s" % (target_type, target_id),
            dataset_id=int(target_id) if target_type == TARGET_DATASET
            else None)
        conn.commit()
    return get_request(request_id)


def review_request(request_id, approve, reviewer, note="", expires_days=None,
                   scope_type=None, scope_id=None, expires_at=None):
    """Approve or deny a request, creating an :class:`AccessGrant` on
    approval, and write an audit event. Returns the updated request.

    Backward compatible: the old ``(id, approve, reviewer, note,
    expires_days)`` call still works and, when no scope is given, the
    grant defaults to the request's own target (image request -> image
    scope, dataset request -> dataset scope) - a strict subset of the
    scoped behaviour, so existing callers keep the same coverage.
    """
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM requests WHERE id=?",
                           (request_id,)).fetchone()
        if row is None:
            return None
        expiry = compute_expiry(expires_days=expires_days,
                                expires_at=expires_at)
        status = STATUS_APPROVED if approve else STATUS_DENIED
        reviewed = _now().isoformat()
        conn.execute(
            "UPDATE requests SET status=?, reviewed_by=?, reviewed_at=?, "
            "review_note=?, expires_at=? WHERE id=?",
            (status, reviewer, reviewed, note or "", expiry, request_id))

        dataset_ctx = (row["target_id"]
                       if row["target_type"] == TARGET_DATASET else None)
        if approve:
            g_scope_type = scope_type or row["target_type"]
            g_scope_id = (scope_id if scope_id is not None
                          else row["target_id"])
            if g_scope_type not in VALID_SCOPES:
                raise ValueError("invalid scope_type: %r" % g_scope_type)
            grant = _insert_grant(
                conn, principal=row["username"], scope_type=g_scope_type,
                scope_id=int(g_scope_id), granted_by=reviewer,
                expires_at=expiry, reason=note or row["reason"],
                request_id=request_id)
            if g_scope_type in (SCOPE_DATASET,) and dataset_ctx is None:
                dataset_ctx = int(g_scope_id)
            _insert_audit(
                conn, actor=reviewer, action=ACTION_APPROVE,
                target_type="request", target_id=request_id,
                detail="grant=%s scope=%s:%s expires=%s"
                % (grant["id"], g_scope_type, g_scope_id, expiry or "never"),
                dataset_id=dataset_ctx)
        else:
            _insert_audit(
                conn, actor=reviewer, action=ACTION_DENY,
                target_type="request", target_id=request_id,
                detail=note or "", dataset_id=dataset_ctx)
        conn.commit()
    return get_request(request_id)


def _covers(target_type, target_id, image_id, dataset_ids):
    """True if an (image|dataset) target covers this image."""
    if target_type == TARGET_IMAGE and target_id == int(image_id):
        return True
    if target_type == TARGET_DATASET and target_id in dataset_ids:
        return True
    return False


def pending_request_for(username, image_id, dataset_ids, project_ids=()):
    """Return a covering pending request for this image, or None.

    Lets the UI show 'request pending' instead of offering to request
    the same image/dataset again. (Requests only target image/dataset,
    so ``project_ids`` is accepted for signature symmetry but unused.)
    """
    dataset_ids = set(int(d) for d in dataset_ids)
    for req in list_requests(username=username, status=STATUS_PENDING):
        if _covers(req["target_type"], req["target_id"], image_id,
                   dataset_ids):
            return req
    return None


# --------------------------------------------------------------------------
# Grants (I3)
# --------------------------------------------------------------------------
def _insert_grant(conn, principal, scope_type, scope_id, granted_by,
                  expires_at=None, reason="", request_id=None):
    grant_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO grants (id, principal, scope_type, scope_id, "
        "granted_by, granted_at, expires_at, reason, request_id, revoked) "
        "VALUES (?,?,?,?,?,?,?,?,?,0)",
        (grant_id, principal, scope_type, int(scope_id), granted_by,
         _now().isoformat(), _normalize_ts(expires_at), reason or "",
         request_id))
    return get_grant(grant_id, _conn=conn)


def create_grant(principal, scope_type, scope_id, granted_by,
                 expires_at=None, reason="", request_id=None):
    """Grant ``principal`` access at a scope, optionally expiring.

    ``expires_at`` accepts a datetime / ISO string / ``YYYY-MM-DD`` or
    None (standing grant). Returns the created grant dict.
    """
    if scope_type not in VALID_SCOPES:
        raise ValueError("invalid scope_type: %r" % scope_type)
    with closing(_connect()) as conn:
        grant = _insert_grant(conn, principal, scope_type, scope_id,
                              granted_by, expires_at, reason, request_id)
        conn.commit()
    return grant


def get_grant(grant_id, _conn=None):
    def _do(conn):
        row = conn.execute("SELECT * FROM grants WHERE id=?",
                           (grant_id,)).fetchone()
        return _grant_dict(row) if row is not None else None
    if _conn is not None:
        return _do(_conn)
    with closing(_connect()) as conn:
        return _do(conn)


def revoke_grant(grant_id, revoked_by, reason=""):
    """Revoke an active grant. Returns the updated grant, or None."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM grants WHERE id=?",
                           (grant_id,)).fetchone()
        if row is None:
            return None
        if not row["revoked"]:
            conn.execute(
                "UPDATE grants SET revoked=1, revoked_by=?, revoked_at=? "
                "WHERE id=?", (revoked_by, _now().isoformat(), grant_id))
            _insert_audit(
                conn, actor=revoked_by, action=ACTION_REVOKE,
                target_type="grant", target_id=grant_id,
                detail=reason or "",
                dataset_id=(row["scope_id"]
                            if row["scope_type"] == SCOPE_DATASET else None))
            conn.commit()
        return get_grant(grant_id)


def _grant_active(row):
    if row["revoked"]:
        return False
    expires_at = row["expires_at"]
    if not expires_at:
        return True
    try:
        return _now() < datetime.fromisoformat(expires_at)
    except ValueError:
        return False


def list_grants(principal=None, active_only=False, scope_type=None,
                scope_id=None):
    """List grants, newest first, with optional filters.

    ``list_grants(principal=user, active_only=True)`` is the user's live
    access set (used by the 'my grants' endpoint).
    """
    clauses, params = [], []
    if principal is not None:
        clauses.append("principal=?")
        params.append(principal)
    if scope_type is not None:
        clauses.append("scope_type=?")
        params.append(scope_type)
    if scope_id is not None:
        clauses.append("scope_id=?")
        params.append(int(scope_id))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM grants%s ORDER BY granted_at DESC, rowid DESC"
            % where, tuple(params)).fetchall()
    grants = [_grant_dict(r) for r in rows]
    if active_only:
        grants = [g for g in grants if g["active"]]
    return grants


def has_approval(username, image_id, dataset_ids, project_ids=()):
    """True iff ``username`` holds an active grant covering this image.

    Coverage: a non-revoked, unexpired grant whose scope is the image's
    own id, one of its parent ``dataset_ids``, or one of its grandparent
    ``project_ids``. ``project_ids`` is an additive optional argument, so
    existing 3-arg image/dataset callers keep working unchanged.
    """
    dataset_ids = set(int(d) for d in dataset_ids)
    project_ids = set(int(p) for p in project_ids)
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM grants WHERE principal=? AND revoked=0",
            (username,)).fetchall()
    for row in rows:
        if not _grant_active(row):
            continue
        st, sid = row["scope_type"], row["scope_id"]
        if st == SCOPE_IMAGE and sid == int(image_id):
            return True
        if st == SCOPE_DATASET and sid in dataset_ids:
            return True
        if st == SCOPE_PROJECT and sid in project_ids:
            return True
    return False


def has_scope_approval(username, dataset_ids=(), project_ids=()):
    """True iff ``username`` holds an active grant on one of these containers.

    The container-level counterpart of :func:`has_approval`, for browsing a
    dataset or project where there is no single image id. A dataset grant
    matches ``dataset_ids``; a project grant matches ``project_ids`` (pass a
    dataset's parent projects so a project-wide grant covers the dataset).
    Image-scoped grants never cover a whole container.
    """
    dataset_ids = set(int(d) for d in dataset_ids)
    project_ids = set(int(p) for p in project_ids)
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM grants WHERE principal=? AND revoked=0",
            (username,)).fetchall()
    for row in rows:
        if not _grant_active(row):
            continue
        st, sid = row["scope_type"], row["scope_id"]
        if st == SCOPE_DATASET and sid in dataset_ids:
            return True
        if st == SCOPE_PROJECT and sid in project_ids:
            return True
    return False


# --------------------------------------------------------------------------
# Requester-side access states (the request form's dataset picker)
# --------------------------------------------------------------------------
ACCESS_OWNER = "owner"
ACCESS_GRANTED = "granted"
ACCESS_PENDING = "pending"
ACCESS_REQUESTABLE = "requestable"


def merge_dataset_rows(rows):
    """Collapse ``(id, name, owner_id, project_id, project_name)`` rows into
    one dict per dataset.

    The view's HQL projection yields one row per project link, so a dataset
    in two projects arrives twice and an orphan once with no project; rows
    from the requester's session and the service account may also overlap.
    The first project seen names the picker group, and every linked project
    id is kept so a project-wide grant still covers the dataset.
    """
    by_id = {}
    for did, name, owner_id, pid, pname in rows:
        item = by_id.get(did)
        if item is None:
            item = by_id[did] = {
                "id": did, "name": name or "Unnamed dataset",
                "owner_id": owner_id, "project_id": None,
                "project_name": None, "project_ids": []}
        if pid is not None:
            if pid not in item["project_ids"]:
                item["project_ids"].append(pid)
            if item["project_id"] is None:
                item["project_id"], item["project_name"] = pid, pname
    return list(by_id.values())


def dataset_access_states(datasets, user_id, grants, pending_requests):
    """Label candidate datasets with the requester's current access state.

    Pure (no DB) so the picker logic unit-tests offline: the view passes in
    the user's active grants and pending requests it already fetched. Each
    item of ``datasets`` is a dict with ``id``, ``owner_id`` and
    ``project_ids``; a copy comes back with ``access`` set to one of
    owner / granted / pending / requestable, in that precedence (owning
    beats a grant, a grant beats a request still in review).
    """
    live = [g for g in grants if g.get("active", True)]
    granted_ds = {g["scope_id"] for g in live
                  if g["scope_type"] == SCOPE_DATASET}
    granted_pr = {g["scope_id"] for g in live
                  if g["scope_type"] == SCOPE_PROJECT}
    pending_ds = {r["target_id"] for r in pending_requests
                  if r["target_type"] == TARGET_DATASET}
    out = []
    for d in datasets:
        item = dict(d)
        if user_id is not None and d.get("owner_id") == user_id:
            item["access"] = ACCESS_OWNER
        elif (d["id"] in granted_ds
              or granted_pr.intersection(d.get("project_ids") or ())):
            item["access"] = ACCESS_GRANTED
        elif d["id"] in pending_ds:
            item["access"] = ACCESS_PENDING
        else:
            item["access"] = ACCESS_REQUESTABLE
        out.append(item)
    return out


# --------------------------------------------------------------------------
# Per-dataset policies (I2)
# --------------------------------------------------------------------------
def set_policy(dataset_id, approver_principals, required_docs=(),
               default_expiry_days=None, auto_expire=True, updated_by=None):
    """Create or replace a dataset's access policy. Returns the policy."""
    approver_principals = [p for p in (approver_principals or []) if p]
    required_docs = [d for d in (required_docs or []) if d]
    bad = [d for d in required_docs if d not in VALID_DOC_TYPES]
    if bad:
        raise ValueError("invalid required_docs: %s" % ", ".join(bad))
    now = _now().isoformat()
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO policies (dataset_id, approver_principals, "
            "required_docs, default_expiry_days, auto_expire, updated_by, "
            "updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(dataset_id) DO UPDATE SET "
            "approver_principals=excluded.approver_principals, "
            "required_docs=excluded.required_docs, "
            "default_expiry_days=excluded.default_expiry_days, "
            "auto_expire=excluded.auto_expire, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (int(dataset_id), json.dumps(approver_principals),
             json.dumps(required_docs),
             int(default_expiry_days) if default_expiry_days else None,
             1 if auto_expire else 0, updated_by, now))
        _insert_audit(
            conn, actor=updated_by, action=ACTION_POLICY_SET,
            target_type="dataset", target_id=str(int(dataset_id)),
            detail="approvers=%s required=%s"
            % (",".join(approver_principals) or "-",
               ",".join(required_docs) or "-"),
            dataset_id=int(dataset_id))
        conn.commit()
    return get_policy(dataset_id)


def get_policy(dataset_id):
    """Return a dataset's policy dict, or None if it has none."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM policies WHERE dataset_id=?",
                           (int(dataset_id),)).fetchone()
        return _policy_dict(row) if row is not None else None


def delete_policy(dataset_id):
    """Remove a dataset's policy. Returns True if one existed."""
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM policies WHERE dataset_id=?",
                           (int(dataset_id),))
        conn.commit()
        return cur.rowcount > 0


def list_policies():
    """All dataset policies, ordered by dataset id."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM policies ORDER BY dataset_id").fetchall()
        return [_policy_dict(r) for r in rows]


def required_docs_for(dataset_id):
    """The required document types for a dataset (``[]`` if no policy)."""
    policy = get_policy(dataset_id)
    return list(policy["required_docs"]) if policy else []


def effective_approvers(dataset_id, default_principals=()):
    """The principals who approve a dataset: its policy's, else the
    supplied default (global-admin/steward fallback). Pure resolution -
    the view still maps these to the live user via username/group."""
    policy = get_policy(dataset_id)
    if policy and policy["approver_principals"]:
        return list(policy["approver_principals"])
    return [p for p in default_principals if p]


def principal_matches(username, group_names, principals):
    """True if a user (by username or any of their group names) is one of
    ``principals``. Approver-principal names are usernames *or* OMERO
    group names, matched here as plain strings (the view resolves the
    user's live group membership and passes ``group_names``)."""
    principals = set(principals or ())
    if username in principals:
        return True
    return any(g in principals for g in (group_names or ()))


def missing_required_docs(required, provided_types):
    """The required document types absent from ``provided_types``."""
    return sorted(set(required or ()) - set(provided_types or ()))


# --------------------------------------------------------------------------
# Audit log (I5)
# --------------------------------------------------------------------------
def _insert_audit(conn, actor, action, target_type=None, target_id=None,
                  detail="", dataset_id=None):
    conn.execute(
        "INSERT INTO audit (actor, action, target_type, target_id, detail, "
        "dataset_id, ts) VALUES (?,?,?,?,?,?,?)",
        (actor, action, target_type,
         str(target_id) if target_id is not None else None,
         detail or "", dataset_id, _now().isoformat()))


def record_audit(actor, action, target_type=None, target_id=None, detail="",
                 dataset_id=None):
    """Append an audit event (e.g. a download, recorded by the view)."""
    with closing(_connect()) as conn:
        _insert_audit(conn, actor, action, target_type, target_id, detail,
                      dataset_id)
        conn.commit()


def list_audit(action=None, actor=None, dataset_ids=None, limit=200):
    """Audit events, newest first.

    ``dataset_ids=None`` returns everything (global-admin view); passing a
    collection restricts to events tagged with those datasets (a steward's
    scoped view of the datasets they approve).
    """
    clauses, params = [], []
    if action is not None:
        clauses.append("action=?")
        params.append(action)
    if actor is not None:
        clauses.append("actor=?")
        params.append(actor)
    if dataset_ids is not None:
        ids = [int(d) for d in dataset_ids]
        if not ids:
            return []
        clauses.append("dataset_id IN (%s)" % ",".join("?" * len(ids)))
        params.extend(ids)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM audit%s ORDER BY id DESC LIMIT ?" % where,
            tuple(params) + (int(limit),)).fetchall()
        return [_audit_dict(r) for r in rows]
