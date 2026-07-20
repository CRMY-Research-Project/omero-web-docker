"""Views for the data-access governance gate (WS-I).

Access model (FR6 / TC-14 / TC-15, extended by WS-I):

* Anyone logged in can *request* download access for an image or a
  dataset, attaching **typed** supporting documents (ethics approval,
  project proposal, data-use agreement).
* Each dataset can carry a **policy** (:func:`store.set_policy`) naming
  who approves it and which document types are mandatory. A request
  routes to that dataset's approvers; a dataset with no policy falls back
  to the configured default approvers (``DOWNLOAD_GATE_DEFAULT_APPROVERS``)
  and global admins - never silently open.
* An approver reviews a request and, on approval, chooses the **scope**
  (this image / its parent dataset / its grandparent project) and
  **expiry** (a date, a duration in days, or standing). That produces an
  :class:`AccessGrant`; ``download_image`` streams original files only to
  admins, the image owner, or holders of an active covering grant.
* Approvers can revoke grants; every request/approve/deny/revoke/
  download/policy_set writes an audit event.

Authorization is resolved *here* (via BlitzGateway ``conn``): dataset ->
policy, image -> dataset -> project lineage, and the user's group
membership. ``store`` stays pure and BlitzGateway-free so it unit-tests
offline.

OMERO's native download endpoints stay blocked for the public user via
the ``omero.web.public.url_filter`` regex in docker-compose. For full
enforcement against *authenticated* users, additionally restrict
``omero.policy.binary_access`` on the server and set the
DOWNLOAD_GATE_SERVICE_USER/PASS env vars so this plugin streams files
through a privileged service account after its own approval check.
"""

import logging
import os

from django.http import (HttpResponse, JsonResponse, StreamingHttpResponse,
                         Http404)
from django.shortcuts import render
from omeroweb.webclient.decorators import login_required

from . import store

logger = logging.getLogger(__name__)

# Supporting documents: modest limits, allowlisted extensions only.
MAX_DOCS = 10
MAX_DOC_MB = int(os.environ.get("DOWNLOAD_GATE_MAX_DOC_MB", 25))
ALLOWED_DOC_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".doc", ".docx", ".txt",
}


def _default_approvers():
    """Principals (usernames/group names) who approve policy-less datasets.

    Read live so tests / redeploys pick up env changes. Global admins are
    always allowed on top of this list.
    """
    raw = os.environ.get("DOWNLOAD_GATE_DEFAULT_APPROVERS", "")
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]


def _default_expiry_days():
    raw = os.environ.get("DOWNLOAD_GATE_DEFAULT_EXPIRY_DAYS", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _error(message, code, status):
    return JsonResponse(
        {"success": False, "error": {"code": code, "message": message}},
        status=status,
    )


def _username(conn):
    return conn.getUser().getName()


def _user_groups(conn):
    """Names of the OMERO groups the current user belongs to."""
    try:
        return [g.getName() for g in conn.getGroupsMemberOf()]
    except Exception:  # pragma: no cover - defensive against gateway shape
        return []


def _is_admin(conn):
    return conn.isAdmin()


def _service_connection():
    """Privileged BlitzGateway if a service account is configured.

    Returns None when unset - the requesting user's own session is used
    instead (sufficient while binary access is open at group level).
    """
    user = os.environ.get("DOWNLOAD_GATE_SERVICE_USER")
    password = os.environ.get("DOWNLOAD_GATE_SERVICE_PASS")
    if not user or not password:
        return None
    from omero.gateway import BlitzGateway
    host = os.environ.get("OMEROHOST", "omeroserver")
    gateway = BlitzGateway(user, password, host=host, port=4064,
                           secure=True)
    if not gateway.connect():
        raise RuntimeError(
            "Download gate service account failed to connect to %s" % host)
    return gateway


def _closing_iter(chunks, gateway):
    """Yield chunks, closing the service connection when the stream
    ends (including client disconnects)."""
    try:
        for chunk in chunks:
            yield chunk
    finally:
        if gateway is not None:
            gateway.close()


# --------------------------------------------------------------------------
# OMERO lineage / approver resolution (view layer - needs conn)
# --------------------------------------------------------------------------
def _image_lineage(conn, image):
    """(dataset_ids, project_ids) for an image, best-effort."""
    dataset_ids, project_ids = [], []
    try:
        for ds in image.listParents():
            dataset_ids.append(ds.getId())
            try:
                for pr in ds.listParents():
                    project_ids.append(pr.getId())
            except Exception:  # pragma: no cover
                pass
    except Exception:  # pragma: no cover
        pass
    return dataset_ids, project_ids


def _dataset_project_ids(conn, dataset_id):
    ds = conn.getObject("Dataset", int(dataset_id))
    if ds is None:
        return []
    try:
        return [pr.getId() for pr in ds.listParents()]
    except Exception:  # pragma: no cover
        return []


def _request_dataset_ids(conn, req):
    """Dataset ids a request touches (its target, or an image's parents)."""
    if req["target_type"] == store.TARGET_DATASET:
        return [req["target_id"]]
    image = conn.getObject("Image", req["target_id"])
    if image is None:
        return []
    dataset_ids, _ = _image_lineage(conn, image)
    return dataset_ids


def _request_scope_targets(conn, req):
    """Resolve image/dataset/project ids an approver may scope a grant to."""
    if req["target_type"] == store.TARGET_DATASET:
        dataset_id = req["target_id"]
        project_ids = _dataset_project_ids(conn, dataset_id)
        return {
            "image_id": None,
            "dataset_id": dataset_id,
            "dataset_ids": [dataset_id],
            "project_id": project_ids[0] if project_ids else None,
            "project_ids": project_ids,
        }
    image = conn.getObject("Image", req["target_id"])
    dataset_ids, project_ids = _image_lineage(conn, image) if image else ([], [])
    return {
        "image_id": req["target_id"],
        "dataset_id": dataset_ids[0] if dataset_ids else None,
        "dataset_ids": dataset_ids,
        "project_id": project_ids[0] if project_ids else None,
        "project_ids": project_ids,
    }


def _effective_approver_principals(dataset_ids):
    """Union of approver principals across the given datasets.

    Uses each dataset's policy, or the default-approver fallback when a
    dataset has none. With no datasets at all, just the defaults.
    """
    principals = set()
    defaults = _default_approvers()
    for did in dataset_ids:
        principals.update(store.effective_approvers(did, defaults))
    if not dataset_ids:
        principals.update(defaults)
    return principals


def _can_review(conn, req):
    """True if the current user may approve/deny this request."""
    if _is_admin(conn):
        return True
    principals = _effective_approver_principals(_request_dataset_ids(conn, req))
    return store.principal_matches(_username(conn), _user_groups(conn),
                                   principals)


def _is_any_approver(conn):
    """True if the user is an approver for the defaults or any policy."""
    username, groups = _username(conn), _user_groups(conn)
    if store.principal_matches(username, groups, _default_approvers()):
        return True
    for pol in store.list_policies():
        if store.principal_matches(username, groups,
                                   pol["approver_principals"]):
            return True
    return False


def _steward_dataset_ids(conn):
    """Datasets the current (non-admin) user approves - for audit scoping."""
    username, groups = _username(conn), _user_groups(conn)
    ids = set()
    for pol in store.list_policies():
        if store.principal_matches(username, groups,
                                   pol["approver_principals"]):
            ids.add(pol["dataset_id"])
    return ids


def _dataset_owner(conn, dataset_id):
    ds = conn.getObject("Dataset", int(dataset_id))
    if ds is None:
        return None, None
    try:
        return ds, ds.getOwner().getId()
    except Exception:  # pragma: no cover
        return ds, None


def _can_manage_policy(conn, dataset_id):
    """Admin, the dataset owner, or a steward already approving it."""
    if _is_admin(conn):
        return True
    _ds, owner_id = _dataset_owner(conn, dataset_id)
    if owner_id is not None and owner_id == conn.getUserId():
        return True
    policy = store.get_policy(dataset_id)
    if policy:
        return store.principal_matches(_username(conn), _user_groups(conn),
                                       policy["approver_principals"])
    return False


def _can_revoke(conn, grant):
    if _is_admin(conn):
        return True
    if grant["granted_by"] == _username(conn):
        return True
    st, sid = grant["scope_type"], grant["scope_id"]
    dataset_ids = []
    if st == store.SCOPE_DATASET:
        dataset_ids = [sid]
    elif st == store.SCOPE_IMAGE:
        image = conn.getObject("Image", sid)
        if image is not None:
            dataset_ids, _ = _image_lineage(conn, image)
    if dataset_ids:
        principals = _effective_approver_principals(dataset_ids)
        return store.principal_matches(_username(conn), _user_groups(conn),
                                       principals)
    return False


def _request_summary(req):
    """Request dict minus server-side detail the UI doesn't need."""
    return {
        "id": req["id"],
        "target_type": req["target_type"],
        "target_id": req["target_id"],
        "reason": req["reason"],
        "documents": req["documents"],
        "documents_detail": req.get("documents_detail", []),
        "status": req["status"],
        "created_at": req["created_at"],
        "reviewed_at": req["reviewed_at"],
        "review_note": req["review_note"],
        "expires_at": req["expires_at"],
    }


# --------------------------------------------------------------------------
# User-facing pages + request submission
# --------------------------------------------------------------------------
@login_required()
def index(request, conn=None, **kwargs):
    """User-facing page: submit a request, track existing ones."""
    return render(request, "omero_download_gate/index.html",
                  {"is_admin": conn.isAdmin(),
                   "can_review": _is_admin(conn) or _is_any_approver(conn)})


@login_required()
def my_requests(request, conn=None, **kwargs):
    requests = [_request_summary(r)
                for r in store.list_requests(username=_username(conn))]
    return JsonResponse({"success": True, "requests": requests})


@login_required()
def create_request(request, conn=None, **kwargs):
    if request.method != "POST":
        return _error("POST required.", "method_not_allowed", 405)

    target_type = request.POST.get("target_type", "")
    if target_type not in store.VALID_TARGETS:
        return _error("target_type must be 'image' or 'dataset'.",
                      "bad_target", 400)
    try:
        target_id = int(request.POST.get("target_id", ""))
    except ValueError:
        return _error("target_id must be an integer.", "bad_target", 400)

    # The target must exist and be visible to the requester
    obj = conn.getObject(target_type.capitalize(), target_id)
    if obj is None:
        return _error(
            "%s %s was not found (or is not visible to you)."
            % (target_type.capitalize(), target_id),
            "target_not_found", 404)

    # One pending request per user+target
    for existing in store.list_requests(status=store.STATUS_PENDING,
                                        username=_username(conn)):
        if existing["target_type"] == target_type \
                and existing["target_id"] == target_id:
            return _error(
                "You already have a pending request for this %s."
                % target_type,
                "duplicate_request", 409)

    # Supporting documents: doc0..docN, each optionally typed via doctypeN
    documents = []
    count = 0
    max_bytes = MAX_DOC_MB * 1024 * 1024
    while request.FILES.get("doc%s" % count) is not None:
        f = request.FILES.get("doc%s" % count)
        doc_type = (request.POST.get("doctype%s" % count) or "").strip().lower()
        doc_type = doc_type or None
        count += 1
        if count > MAX_DOCS:
            return _error("At most %s supporting documents are allowed."
                          % MAX_DOCS, "too_many_docs", 400)
        ext = os.path.splitext(f.name or "")[1].lower()
        if ext not in ALLOWED_DOC_EXTENSIONS:
            return _error(
                "'%s': unsupported document type. Allowed: %s"
                % (f.name, ", ".join(sorted(ALLOWED_DOC_EXTENSIONS))),
                "bad_doc_type", 400)
        if doc_type is not None and doc_type not in store.VALID_DOC_TYPES:
            return _error(
                "'%s': document category must be one of %s."
                % (doc_type, ", ".join(store.VALID_DOC_TYPES)),
                "bad_doc_category", 400)
        if f.size > max_bytes:
            return _error(
                "'%s' exceeds the %s MiB document limit."
                % (f.name, MAX_DOC_MB),
                "doc_too_large", 413)
        documents.append((f.name, f.chunks(), doc_type))

    # Enforce the dataset policy's required document types (I4)
    if target_type == store.TARGET_DATASET:
        required = store.required_docs_for(target_id)
    else:
        required = set()
        for did, in [(d,) for d in _image_lineage(conn, obj)[0]]:
            required.update(store.required_docs_for(did))
        required = sorted(required)

    try:
        req = store.create_request(
            username=_username(conn),
            user_id=conn.getUserId(),
            target_type=target_type,
            target_id=target_id,
            reason=request.POST.get("reason", "").strip()[:2000],
            documents=documents,
            required_docs=required,
        )
    except store.MissingDocumentError as exc:
        return _error(
            "This %s requires document(s): %s. Missing: %s."
            % (target_type, ", ".join(required), ", ".join(exc.missing)),
            "missing_required_docs", 400)

    logger.info("Download request %s created by %s for %s %s",
                req["id"], _username(conn), target_type, target_id)
    return JsonResponse({"success": True,
                         "request": _request_summary(req)})


# --------------------------------------------------------------------------
# Review (approver or global admin)
# --------------------------------------------------------------------------
@login_required()
def review(request, conn=None, **kwargs):
    """Review page - approvers and admins only."""
    if not (_is_admin(conn) or _is_any_approver(conn)):
        return _error("Approvers only.", "forbidden", 403)
    return render(request, "omero_download_gate/review.html",
                  {"is_admin": _is_admin(conn),
                   "doc_types": list(store.VALID_DOC_TYPES)})


@login_required()
def review_list(request, conn=None, **kwargs):
    if not (_is_admin(conn) or _is_any_approver(conn)):
        return _error("Approvers only.", "forbidden", 403)
    pending = store.list_requests(status=store.STATUS_PENDING)
    reviewed = [r for r in store.list_requests()
                if r["status"] != store.STATUS_PENDING]
    # A steward only sees requests for datasets they approve.
    if not _is_admin(conn):
        pending = [r for r in pending if _can_review(conn, r)]
        reviewed = [r for r in reviewed if _can_review(conn, r)]
    return JsonResponse({
        "success": True,
        "pending": pending,
        "reviewed": reviewed[:50],
    })


@login_required()
def review_action(request, conn=None, **kwargs):
    if request.method != "POST":
        return _error("POST required.", "method_not_allowed", 405)

    request_id = request.POST.get("request_id", "")
    action = request.POST.get("action", "")
    if action not in ("approve", "deny"):
        return _error("action must be 'approve' or 'deny'.",
                      "bad_action", 400)

    req = store.get_request(request_id)
    if req is None:
        return _error("Request not found.", "not_found", 404)
    if not _can_review(conn, req):
        return _error(
            "You are not an approver for this request's dataset.",
            "forbidden", 403)

    # Expiry choice (I3): standing, an explicit date, or a duration.
    standing = request.POST.get("standing", "") in ("1", "true", "yes", "on")
    expires_at = (request.POST.get("expires_at") or "").strip() or None
    expires_days = request.POST.get("expires_days") or None
    if expires_days is not None:
        try:
            expires_days = int(expires_days)
            if expires_days <= 0:
                raise ValueError()
        except ValueError:
            return _error("expires_days must be a positive integer.",
                          "bad_expiry", 400)

    # Scope choice (I3): image / dataset / project, resolved via lineage.
    scope = (request.POST.get("scope") or "").strip().lower() or None
    scope_id = request.POST.get("scope_id") or None
    scope_type = None
    if action == "approve":
        if scope is not None and scope not in store.VALID_SCOPES:
            return _error("scope must be one of %s."
                          % ", ".join(store.VALID_SCOPES), "bad_scope", 400)
        targets = _request_scope_targets(conn, req)
        if scope is not None:
            scope_type = scope
            if scope_id is not None:
                try:
                    scope_id = int(scope_id)
                except ValueError:
                    return _error("scope_id must be an integer.",
                                  "bad_scope", 400)
            else:
                scope_id = {
                    store.SCOPE_IMAGE: targets["image_id"],
                    store.SCOPE_DATASET: targets["dataset_id"],
                    store.SCOPE_PROJECT: targets["project_id"],
                }.get(scope)
                if scope_id is None:
                    return _error(
                        "Could not resolve a %s id for this request; pass "
                        "scope_id explicitly." % scope, "bad_scope", 400)
        # Fall back to a policy default expiry if the approver gave none.
        if not standing and not expires_at and not expires_days:
            for did in targets["dataset_ids"]:
                policy = store.get_policy(did)
                if policy and policy["default_expiry_days"]:
                    expires_days = policy["default_expiry_days"]
                    break
            if expires_days is None:
                expires_days = _default_expiry_days()

    req = store.review_request(
        request_id,
        approve=(action == "approve"),
        reviewer=_username(conn),
        note=request.POST.get("note", "").strip()[:2000],
        expires_days=(None if standing else expires_days),
        scope_type=scope_type,
        scope_id=scope_id if isinstance(scope_id, int) else None,
        expires_at=(None if standing else expires_at),
    )
    if req is None:
        return _error("Request not found.", "not_found", 404)
    logger.info("Download request %s %s by %s",
                request_id, req["status"], _username(conn))
    return JsonResponse({"success": True, "request": req})


# --------------------------------------------------------------------------
# Dataset policies (I2)
# --------------------------------------------------------------------------
def _parse_principals(request, field, item_field):
    values = []
    raw = request.POST.get(field, "")
    values.extend(p.strip() for p in raw.replace("\n", ",").split(",")
                  if p.strip())
    values.extend(v.strip() for v in request.POST.getlist(item_field)
                  if v.strip())
    # de-dupe, preserve order
    seen, out = set(), []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


@login_required()
def dataset_policy(request, dataset_id, conn=None, **kwargs):
    """GET or SET a dataset's access policy (owner / steward / admin)."""
    dataset_id = int(dataset_id)
    ds = conn.getObject("Dataset", dataset_id)
    if ds is None:
        return _error("Dataset %s not found (or not visible to you)."
                      % dataset_id, "not_found", 404)
    if not _can_manage_policy(conn, dataset_id):
        return _error(
            "Only the dataset owner, a steward approver, or an admin may "
            "view or change this policy.", "forbidden", 403)

    if request.method == "GET":
        return JsonResponse({
            "success": True,
            "dataset_id": dataset_id,
            "policy": store.get_policy(dataset_id),
            "default_approvers": _default_approvers(),
            "doc_types": list(store.VALID_DOC_TYPES),
        })

    if request.method == "POST":
        approver_principals = _parse_principals(
            request, "approver_principals", "approver")
        required_docs = _parse_principals(
            request, "required_docs", "required_doc")
        default_expiry_days = request.POST.get("default_expiry_days") or None
        if default_expiry_days is not None:
            try:
                default_expiry_days = int(default_expiry_days)
            except ValueError:
                return _error("default_expiry_days must be an integer.",
                              "bad_policy", 400)
        auto_expire = request.POST.get("auto_expire", "1") not in \
            ("0", "false", "no", "off")
        try:
            policy = store.set_policy(
                dataset_id,
                approver_principals=approver_principals,
                required_docs=required_docs,
                default_expiry_days=default_expiry_days,
                auto_expire=auto_expire,
                updated_by=_username(conn))
        except ValueError as exc:
            return _error(str(exc), "bad_policy", 400)
        logger.info("Dataset %s policy set by %s", dataset_id,
                    _username(conn))
        return JsonResponse({"success": True, "policy": policy})

    return _error("GET or POST required.", "method_not_allowed", 405)


# --------------------------------------------------------------------------
# Grants (I3)
# --------------------------------------------------------------------------
@login_required()
def my_grants(request, conn=None, **kwargs):
    """The current user's grants (active only unless ?active=0)."""
    active = request.GET.get("active", "1") not in ("0", "false", "no")
    grants = store.list_grants(principal=_username(conn), active_only=active)
    return JsonResponse({"success": True, "grants": grants})


@login_required()
def grants_list(request, conn=None, **kwargs):
    """List grants for review/revocation (admin or approver)."""
    if not (_is_admin(conn) or _is_any_approver(conn)):
        return _error("Approvers only.", "forbidden", 403)
    principal = request.GET.get("principal") or None
    active = request.GET.get("active", "0") not in ("0", "false", "no")
    grants = store.list_grants(principal=principal, active_only=active)
    if not _is_admin(conn):
        grants = [g for g in grants if _can_revoke(conn, g)]
    return JsonResponse({"success": True, "grants": grants})


@login_required()
def grant_revoke(request, conn=None, **kwargs):
    if request.method != "POST":
        return _error("POST required.", "method_not_allowed", 405)
    grant_id = request.POST.get("grant_id", "")
    grant = store.get_grant(grant_id)
    if grant is None:
        return _error("Grant not found.", "not_found", 404)
    if not _can_revoke(conn, grant):
        return _error("You may not revoke this grant.", "forbidden", 403)
    updated = store.revoke_grant(grant_id, _username(conn),
                                 request.POST.get("reason", "").strip()[:2000])
    logger.info("Grant %s revoked by %s", grant_id, _username(conn))
    return JsonResponse({"success": True, "grant": updated})


# --------------------------------------------------------------------------
# Audit log (I5)
# --------------------------------------------------------------------------
@login_required()
def audit_log(request, conn=None, **kwargs):
    """Governance audit log (admin sees all; steward sees their datasets)."""
    action = request.GET.get("action") or None
    if action is not None and action not in store.VALID_ACTIONS:
        return _error("Unknown action filter.", "bad_action", 400)
    try:
        limit = min(int(request.GET.get("limit", 200) or 200), 1000)
    except ValueError:
        limit = 200

    if _is_admin(conn):
        events = store.list_audit(action=action, limit=limit)
    else:
        dataset_ids = _steward_dataset_ids(conn)
        if not dataset_ids:
            return _error("Approvers only.", "forbidden", 403)
        events = store.list_audit(action=action, dataset_ids=dataset_ids,
                                  limit=limit)
    return JsonResponse({"success": True, "events": events})


# --------------------------------------------------------------------------
# Supporting documents
# --------------------------------------------------------------------------
@login_required()
def download_doc(request, request_id, filename, conn=None, **kwargs):
    """Serve a supporting document to admins, approvers, or the owner."""
    req = store.get_request(request_id)
    if req is None:
        raise Http404()
    if req["username"] != _username(conn) and not _can_review(conn, req):
        return _error("Not authorised for this request.", "forbidden", 403)
    filename = store.safe_filename(filename)
    if filename not in req["documents"]:
        raise Http404()
    path = os.path.join(store.docs_dir(request_id), filename)
    if not os.path.exists(path):
        raise Http404()

    def file_iter(p, buf=1024 * 1024):
        with open(p, "rb") as f:
            while True:
                chunk = f.read(buf)
                if not chunk:
                    break
                yield chunk

    # Always served as a download - never rendered - so an uploaded
    # HTML/SVG payload can't execute in the portal's origin.
    response = StreamingHttpResponse(file_iter(path),
                                     content_type="application/octet-stream")
    response["Content-Length"] = os.path.getsize(path)
    response["Content-Disposition"] = 'attachment; filename="%s"' % filename
    response["X-Content-Type-Options"] = "nosniff"
    return response


# --------------------------------------------------------------------------
# Gated downloads
# --------------------------------------------------------------------------
@login_required()
def list_image_files(request, image_id, conn=None, **kwargs):
    """List an image's original files so the UI can offer downloads."""
    image = conn.getObject("Image", int(image_id))
    if image is None:
        return _error("Image %s not found." % image_id, "not_found", 404)
    fileset = image.getFileset()
    files = list(fileset.listFiles()) if fileset is not None else []
    return JsonResponse({
        "success": True,
        "files": [{"id": f.getId(), "name": f.getName(),
                   "size": f.getSize()} for f in files],
    })


def _download_decision(conn, image, dataset_ids, project_ids):
    """Return (allowed, reason) for the current user + image.

    reason in {admin, owner, approved, not_approved} - kept to this set so
    the landing page's typed ``DownloadStatus.reason`` never breaks.
    """
    if conn.isAdmin():
        return True, "admin"
    if image.getOwner().getId() == conn.getUserId():
        return True, "owner"
    if store.has_approval(_username(conn), image.getId(), dataset_ids,
                          project_ids):
        return True, "approved"
    return False, "not_approved"


@login_required()
def download_status(request, image_id, conn=None, **kwargs):
    """Per-image download eligibility, for the landing-page button.

    Returns can_download + reason, plus whether a pending request already
    exists (so the UI shows 'request pending' instead of re-requesting).
    """
    image_id = int(image_id)
    image = conn.getObject("Image", image_id)
    if image is None:
        return _error("Image %s not found (or not visible to you)."
                      % image_id, "not_found", 404)
    dataset_ids, project_ids = _image_lineage(conn, image)
    allowed, reason = _download_decision(conn, image, dataset_ids,
                                         project_ids)
    pending = store.pending_request_for(_username(conn), image_id,
                                        dataset_ids)
    return JsonResponse({
        "success": True,
        "image_id": image_id,
        "can_download": allowed,
        "reason": reason,
        "has_pending_request": pending is not None,
        "dataset_ids": dataset_ids,
        "project_ids": project_ids,
    })


def _xaccel_config():
    """(managed_root, internal_prefix) when X-Accel offload is configured,
    else (None, None). DOWNLOAD_GATE_XACCEL_ROOT is the managed-repo root the
    NGINX 'internal' location aliases; DOWNLOAD_GATE_XACCEL_INTERNAL is that
    location (default /_protected). See nginx/reverse_proxy.conf.example."""
    root = os.environ.get("DOWNLOAD_GATE_XACCEL_ROOT", "").strip()
    prefix = os.environ.get("DOWNLOAD_GATE_XACCEL_INTERNAL",
                            "/_protected").strip()
    return (root, prefix) if root else (None, None)


def _select_original_file(image, file_id=None):
    """The requested original file of an image (by file_id, else the first),
    or None. Metadata only - resolvable from the requesting user's view."""
    fileset = image.getFileset() if image is not None else None
    files = list(fileset.listFiles()) if fileset is not None else []
    if not files:
        return None
    if file_id:
        for f in files:
            if str(f.getId()) == str(file_id):
                return f
        return None
    return files[0]


@login_required()
def download_image(request, image_id, conn=None, **kwargs):
    """Serve an original file of an image - the gated endpoint.

    Fast path (WS-H / H2): when the managed repo is mounted and
    DOWNLOAD_GATE_XACCEL_ROOT is set, hand byte-serving to NGINX via
    X-Accel-Redirect (sendfile + Range/resume, zero bytes through Python).
    Fallback: chunk-stream via BlitzGateway (service account if configured).
    """
    image_id = int(image_id)
    image = conn.getObject("Image", image_id)
    if image is None:
        return _error("Image %s not found (or not visible to you)."
                      % image_id, "not_found", 404)

    dataset_ids, project_ids = _image_lineage(conn, image)
    allowed, reason = _download_decision(conn, image, dataset_ids,
                                         project_ids)
    if not allowed:
        return _error(
            "Download not approved. Submit a request from the "
            "Downloads page and wait for approval.",
            "not_approved", 403)

    # Fast path (WS-H / H2): hand byte-serving to NGINX via X-Accel-Redirect.
    # The approval check above is the gate; the /_protected/ location is
    # 'internal', unreachable except via this redirect, and the target path is
    # traversal-guarded in store.xaccel_internal_uri.
    xroot, xprefix = _xaccel_config()
    if xroot:
        xtarget = _select_original_file(image, request.GET.get("file_id"))
        if xtarget is not None:
            uri = store.xaccel_internal_uri(
                xroot, xprefix, xtarget.getPath(), xtarget.getName())
            if uri:
                store.record_audit(
                    actor=_username(conn), action=store.ACTION_DOWNLOAD,
                    target_type="image", target_id=image_id,
                    detail="file=%s via=x-accel reason=%s"
                    % (xtarget.getId(), reason),
                    dataset_id=dataset_ids[0] if dataset_ids else None)
                logger.info("Gated download (X-Accel): user=%s image=%s "
                            "file=%s", _username(conn), image_id,
                            xtarget.getId())
                response = HttpResponse(
                    content_type="application/octet-stream")
                response["X-Accel-Redirect"] = uri
                response["Content-Disposition"] = \
                    'attachment; filename="%s"' \
                    % store.safe_filename(xtarget.getName())
                return response
            logger.warning(
                "X-Accel configured but file %s resolved outside the root; "
                "falling back to streaming.", xtarget.getId())

    gateway = None
    try:
        gateway = _service_connection()
        if gateway is not None:
            source_image = gateway.getObject("Image", image_id)
            if source_image is None:
                return _error(
                    "Service account cannot see image %s; check its "
                    "group membership." % image_id,
                    "service_misconfigured", 500)
        else:
            source_image = image

        fileset = source_image.getFileset()
        files = list(fileset.listFiles()) if fileset is not None else []
        if not files:
            return _error(
                "No original files are attached to image %s." % image_id,
                "no_files", 404)

        target = files[0]
        file_id = request.GET.get("file_id")
        if file_id:
            matches = [f for f in files
                       if str(f.getId()) == str(file_id)]
            if not matches:
                return _error("File %s does not belong to image %s."
                              % (file_id, image_id), "bad_file", 404)
            target = matches[0]

        store.record_audit(
            actor=_username(conn), action=store.ACTION_DOWNLOAD,
            target_type="image", target_id=image_id,
            detail="file=%s reason=%s" % (target.getId(), reason),
            dataset_id=dataset_ids[0] if dataset_ids else None)
        logger.info("Gated download: user=%s image=%s file=%s",
                    _username(conn), image_id, target.getId())
        response = StreamingHttpResponse(
            _closing_iter(target.getFileInChunks(buf=1024 * 1024),
                          gateway),
            content_type="application/octet-stream")
        response["Content-Length"] = target.getSize()
        response["Content-Disposition"] = \
            'attachment; filename="%s"' \
            % store.safe_filename(target.getName())
        gateway = None  # ownership handed to _closing_iter
        return response
    finally:
        if gateway is not None:
            gateway.close()
