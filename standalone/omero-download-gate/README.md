# omero-download-gate

OMERO.web plugin implementing the CRM **data-access governance** workflow
(FR6 / TC-14 / TC-15, extended by Phase-2 **WS-I**):

1. An authenticated user **requests** download access to an **image** or a
   whole **dataset**, attaching **typed** supporting documents (ethics
   approval, project proposal, data-use agreement).
2. The request routes to that dataset's **approvers** — configured
   per-dataset by its owner/steward (a `DatasetPolicy`), or the default
   approvers + global admins when a dataset has no policy (never silently
   open).
3. An approver reviews and, on approval, chooses the **scope** (this image /
   its parent dataset / its grandparent project) and **expiry** (a date, a
   duration in days, or standing). That produces an **`AccessGrant`**.
4. Approved users download original files through the gated endpoint
   `download/image/<id>/`. Admins and image owners bypass the check.
5. Approvers can **revoke** grants; every request/approve/deny/revoke/
   download/policy-change is written to an append-only **audit log**.

## State storage (WS-I)

State lives in a single **SQLite** database (WAL mode) at
`DOWNLOAD_GATE_DIR/gate.db` (default dir `/opt/omero/web/download_gate` —
mount a volume there). This replaced the Phase-1 per-request JSON store,
whose `has_approval` scanned *every* request file on *every* call — an O(n)
walk hit on each `download_status`. SQLite gives indexed lookups,
transactions and safe concurrent reads (WAL) at 50+ users, with **no
OMERO.web Django database required** — the gate stays self-contained on its
own volume.

Existing Phase-1 `requests/*.json` are **auto-migrated** into SQLite on first
start (approved legacy requests also seed an equivalent grant), so no access
state is lost on upgrade.

**Postgres upgrade path:** every caller goes through the `store` facade, so
swapping `_connect`/the SQL dialect (or backing the same public functions
with Django-ORM models `DownloadRequest`/`AccessGrant`/`DatasetPolicy`/
`AuditEvent`) is invisible to `views.py`.

### Data model

| Entity | Meaning |
|---|---|
| `requests` | a user's ask for access, plus typed documents |
| `grants` | what an approver granted: `scope (image/dataset/project)` + expiry, split from the request so scope ≠ what was asked for |
| `policies` | per-dataset approver principals + required doc types + default expiry |
| `audit` | append-only governance log (actor, action, target, dataset, ts) |

`store.py` stays pure — importable **without** `omero.gateway` — so it
unit-tests offline. All OMERO lineage (image→dataset→project) and group
membership resolution happen in `views.py`, which feeds resolved ids/
principals into the pure store functions.

## Approvers & authorization

An **approver principal** in a `DatasetPolicy` is either an **OMERO
username** or an **OMERO group name** (e.g. `steward1` or
`group:data-stewards` — matched as a plain string against the user's live
group membership resolved in the view). A request can be reviewed by:

* a **global admin**, or
* a principal named by the target dataset's policy, or
* (policy-less datasets) a principal in `DOWNLOAD_GATE_DEFAULT_APPROVERS`.

This is what lets a **data steward who is *not* a global admin** approve
downloads — see [`docs/iam-architecture.md`](../../../docs/iam-architecture.md)
for the OMERO **light-administrator** role that backs it, and
[`omero-docker/scripts/provision_iam.sh`](../../../omero-docker/scripts/provision_iam.sh)
to create the steward account + groups.

## Endpoints

| Route | View | Who |
|---|---|---|
| `/` | `index` | any logged-in user |
| `request/` (POST) | `create_request` | any user (enforces the dataset's required docs) |
| `requests/mine/` | `my_requests` | the requester |
| `review/` | `review` | approver or admin |
| `review/list/` | `review_list` | approver (their datasets) or admin (all) |
| `review/action/` (POST) | `review_action` | approver of the request's dataset, or admin — picks scope + expiry |
| `policy/dataset/<id>/` (GET/POST) | `dataset_policy` | dataset owner, steward approver, or admin |
| `grants/mine/` | `my_grants` | grant holder |
| `grants/` | `grants_list` | approver or admin |
| `grants/revoke/` (POST) | `grant_revoke` | grantor, dataset approver, or admin |
| `audit/` | `audit_log` | admin (all) / steward (their datasets) |
| `doc/<request_id>/<file>` | `download_doc` | request owner, approver, or admin |
| `files/image/<id>/` | `list_image_files` | any user |
| `status/image/<id>/` | `download_status` | any user (drives the landing-page button) |
| `download/image/<id>/` | `download_image` | admin, owner, or active-grant holder |

`download_status` keeps its existing contract for the Svelte landing page
(`success`, `image_id`, `can_download`, `reason`, `has_pending_request`,
`dataset_ids`) and additively returns `project_ids`.

## Enforcement

Both layers are on in `omero-docker/docker-compose.yml`:

* **Anonymous visitors:** `omero.web.public.url_filter` excludes every route
  that streams an original file or exposes its paths/metadata
  (`webgateway/archived_files`, `download_as`, `original_file_paths`;
  `webclient/get_original_file`, `download_original_file`,
  `download_orig_metadata`, `download_placeholder`).
* **Authenticated users:** `omero.policy.binary_access=-read,+write,+image`
  on OMERO.server refuses native original-file reads to anyone who cannot
  update the file, i.e. everyone but its owner and full admins (`-write`
  would refuse those too).

The gate is therefore the only download path for everyone else, and it
serves approved bytes **without** OMERO's RawFileStore, which the lockdown
would refuse: via NGINX `X-Accel-Redirect` when `DOWNLOAD_GATE_XACCEL_ROOT`
is set, otherwise from the read-only managed-repository mount at
`DOWNLOAD_GATE_DIRECT_ROOT`. The BlitzGateway stream is the last resort and
fails early with a clear 503 when the server refuses it.

The service account only *reads* metadata across groups, so a light
administrator holding no privileges is enough:
`omero-docker/scripts/provision_iam.sh gate` creates one.

## Associated-image guard

`omero_download_gate.middleware.AssociatedImageGuard` (registered in
`01-default-webapps.omero`) keeps scanner label and macro images from
anonymous sessions. They share the slide's fileset and group, so OMERO
permissions cannot hide them. Every route that resolves an image id
returns 403 for them, and thumbnail batches drop them; logged-in users are
unaffected. Names are looked up through the service account and cached for
an hour. If the lookup fails the request is refused (503), never served.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `DOWNLOAD_GATE_DIR` | `/opt/omero/web/download_gate` | SQLite DB + document storage (volume-backed) |
| `DOWNLOAD_GATE_MAX_DOC_MB` | `25` | per-document size cap |
| `DOWNLOAD_GATE_DEFAULT_APPROVERS` | _(unset)_ | comma/newline list of usernames/group names who approve **policy-less** datasets (admins always may) |
| `DOWNLOAD_GATE_DEFAULT_EXPIRY_DAYS` | _(unset)_ | fallback grant expiry when neither the approver nor a dataset policy specifies one |
| `DOWNLOAD_GATE_SERVICE_USER` | _(unset)_ | cross-group read-only account (light admin, no privileges); required for out-of-group grants and the associated-image guard |
| `DOWNLOAD_GATE_SERVICE_PASS` | _(unset)_ | its password |
| `DOWNLOAD_GATE_DIRECT_ROOT` | _(unset)_ | read-only mount of the managed repository (e.g. `/OMERO/ManagedRepository`) for direct-read downloads |
| `DOWNLOAD_GATE_XACCEL_ROOT` / `_INTERNAL` | _(unset)_ / `/_protected` | NGINX X-Accel offload (takes precedence over direct reads) |
| `OMEROHOST` | `omeroserver` | server host for the service account |

## Tests

Offline unit tests (no OMERO/Django needed), run with `python -m pytest tests/`:

* `tests/test_store.py`: request lifecycle, grant scope coverage
  (image/dataset/project), expiry, revocation, per-dataset policy CRUD +
  approver resolution, required-doc enforcement, audit log + dataset
  scoping, legacy-JSON migration, and the traversal guards behind X-Accel
  and direct-read paths.
* `tests/test_assoc.py`: the associated-image guard's rules (name rule,
  session classification, image-id extraction per route, verdict cache).

```sh
PYTHONPATH=. python -m pytest tests -q
```
