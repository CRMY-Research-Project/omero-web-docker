"""Unit tests for the download-gate request store.

Pure-filesystem tests - no OMERO or Django required, so they run in
plain pytest and in CI without omero-test-infra.
"""

import pytest

from omero_download_gate import store


@pytest.fixture(autouse=True)
def gate_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DOWNLOAD_GATE_DIR", str(tmp_path))
    return tmp_path


def make_request(username="alice", user_id=5, target_type="dataset",
                 target_id=12, reason="ethics ref 42", documents=()):
    return store.create_request(username, user_id, target_type,
                                target_id, reason, list(documents))


def test_create_request_is_pending():
    req = make_request()
    assert req["status"] == store.STATUS_PENDING
    assert req["target_type"] == "dataset"
    assert req["target_id"] == 12
    assert store.get_request(req["id"])["reason"] == "ethics ref 42"


def test_create_request_rejects_bad_target_type():
    with pytest.raises(ValueError):
        make_request(target_type="project")


def test_documents_written_and_collisions_renamed(gate_dir):
    req = make_request(documents=[
        ("proposal.pdf", [b"abc", b"def"]),
        ("proposal.pdf", [b"xyz"]),
    ])
    assert sorted(req["documents"]) == ["_proposal.pdf", "proposal.pdf"]
    first = gate_dir / "docs" / req["id"] / "proposal.pdf"
    assert first.read_bytes() == b"abcdef"


def test_approval_covers_image_via_parent_dataset():
    req = make_request()
    assert not store.has_approval("alice", 99, [12])
    store.review_request(req["id"], True, "root", "ok")
    assert store.has_approval("alice", 99, [12])
    assert not store.has_approval("alice", 99, [13])
    assert not store.has_approval("bob", 99, [12])


def test_direct_image_approval():
    req = make_request(username="bob", target_type="image", target_id=99)
    store.review_request(req["id"], True, "root")
    assert store.has_approval("bob", 99, [])


def test_denied_request_grants_nothing():
    req = make_request(username="carol", target_type="image",
                       target_id=99)
    store.review_request(req["id"], False, "root", "insufficient docs")
    assert not store.has_approval("carol", 99, [])
    assert store.get_request(req["id"])["status"] == store.STATUS_DENIED


def test_expiry_in_past_is_inactive():
    # Expiry now lives on the grant, not the request (WS-I I3): a past-dated
    # grant does not grant access; a future-dated one does.
    store.create_grant("alice", store.SCOPE_DATASET, 12, "root",
                       expires_at="2000-01-01T00:00:00+00:00")
    assert not store.has_approval("alice", 99, [12])
    store.create_grant("alice", store.SCOPE_DATASET, 13, "root",
                       expires_at="2999-01-01T00:00:00+00:00")
    assert store.has_approval("alice", 99, [13])


def test_listing_filters():
    a = make_request(username="alice")
    b = make_request(username="bob", target_type="image", target_id=7)
    store.review_request(b["id"], True, "root")
    assert len(store.list_requests()) == 2
    assert [r["id"] for r in store.list_requests(username="alice")] \
        == [a["id"]]
    assert [r["id"] for r in
            store.list_requests(status=store.STATUS_PENDING)] == [a["id"]]


def test_request_path_rejects_traversal():
    assert store._request_path("../../etc/passwd") is None
    assert store._request_path("ABC") is None
    assert store._request_path("") is None


def test_safe_filename_strips_paths():
    assert store.safe_filename("../../evil.sh") == "evil.sh"
    assert store.safe_filename("a b/c\\d.pdf") == "d.pdf"
    assert store.safe_filename("") == "unnamed"


def test_review_unknown_request_returns_none():
    assert store.review_request("0" * 32, True, "root") is None


def test_pending_request_for_matches_image_and_dataset():
    # image-targeted pending request
    img_req = make_request(username="dave", target_type="image",
                           target_id=55)
    found = store.pending_request_for("dave", 55, [])
    assert found is not None and found["id"] == img_req["id"]
    # different user sees nothing
    assert store.pending_request_for("erin", 55, []) is None
    # dataset-targeted pending request covers a child image
    make_request(username="erin", target_type="dataset", target_id=88)
    assert store.pending_request_for("erin", 999, [88]) is not None
    assert store.pending_request_for("erin", 999, [77]) is None


def test_pending_request_for_ignores_reviewed():
    req = make_request(username="frank", target_type="image",
                       target_id=61)
    store.review_request(req["id"], True, "root")
    # once approved it is no longer "pending"
    assert store.pending_request_for("frank", 61, []) is None


# --- WS-I: grants, scopes, policies, audit, migration ---------------------

def test_grant_scopes_image_dataset_project():
    store.create_grant("gale", store.SCOPE_IMAGE, 100, "root")
    assert store.has_approval("gale", 100, [], [])
    assert not store.has_approval("gale", 101, [], [])
    store.create_grant("gale", store.SCOPE_DATASET, 200, "root")
    assert store.has_approval("gale", 999, [200], [])
    store.create_grant("gale", store.SCOPE_PROJECT, 300, "root")
    assert store.has_approval("gale", 999, [201], [300])
    assert not store.has_approval("gale", 999, [201], [301])


def test_revoke_grant_removes_access():
    g = store.create_grant("hank", store.SCOPE_DATASET, 12, "root")
    assert store.has_approval("hank", 5, [12])
    updated = store.revoke_grant(g["id"], "root", "study closed")
    assert updated["revoked"] is True
    assert not store.has_approval("hank", 5, [12])


def test_create_grant_rejects_bad_scope():
    with pytest.raises(ValueError):
        store.create_grant("ivy", "galaxy", 1, "root")


def test_standing_grant_never_expires():
    store.create_grant("jo", store.SCOPE_IMAGE, 7, "root")  # no expiry
    assert store.has_approval("jo", 7, [])


def test_set_and_get_policy():
    pol = store.set_policy(88, ["steward1", "group:data-stewards"],
                           required_docs=[store.DOC_ETHICS],
                           default_expiry_days=180, updated_by="root")
    assert pol["approver_principals"] == ["steward1", "group:data-stewards"]
    assert pol["required_docs"] == ["ethics"]
    assert store.get_policy(88)["default_expiry_days"] == 180
    assert store.required_docs_for(88) == ["ethics"]
    assert store.delete_policy(88) is True
    assert store.get_policy(88) is None


def test_set_policy_rejects_bad_doc_type():
    with pytest.raises(ValueError):
        store.set_policy(1, ["steward1"], required_docs=["passport"])


def test_effective_approvers_falls_back_to_default():
    assert store.effective_approvers(404, ["admin"]) == ["admin"]
    store.set_policy(9, ["steward1"], updated_by="root")
    assert store.effective_approvers(9, ["admin"]) == ["steward1"]


def test_principal_matches_username_or_group():
    assert store.principal_matches("steward1", [], ["steward1"])
    # a user whose group name is not among the principals does not match
    assert not store.principal_matches("bob", ["histopath-a"],
                                       ["group:data-stewards"])
    # group principals are matched as plain strings the view resolves + passes
    assert store.principal_matches("bob", ["group:data-stewards"],
                                   ["group:data-stewards"])
    assert not store.principal_matches("bob", ["other"], ["steward1"])


def test_missing_required_docs():
    assert store.missing_required_docs(["ethics", "dua"], ["ethics"]) == ["dua"]
    assert store.missing_required_docs([], []) == []


def test_create_request_enforces_required_docs():
    with pytest.raises(store.MissingDocumentError):
        store.create_request("kim", 1, "dataset", 12, "r",
                             [("a.pdf", [b"x"])], required_docs=["ethics"])
    # providing the typed document satisfies the requirement
    req = store.create_request("kim", 1, "dataset", 12, "r",
                               [("e.pdf", [b"x"], "ethics")],
                               required_docs=["ethics"])
    assert req["documents_detail"][0]["type"] == "ethics"


def test_review_with_explicit_scope_and_expiry():
    req = make_request(username="lee", target_type="dataset", target_id=20)
    out = store.review_request(req["id"], True, "steward1",
                               scope_type=store.SCOPE_PROJECT, scope_id=77,
                               expires_days=10)
    assert out["status"] == store.STATUS_APPROVED
    # granted at project scope -> covers any image under project 77
    assert store.has_approval("lee", 500, [21], [77])


def test_audit_log_records_and_filters():
    req = make_request(username="mae", target_type="image", target_id=42)
    store.review_request(req["id"], True, "steward1")
    actions = [e["action"] for e in store.list_audit()]
    assert store.ACTION_REQUEST in actions
    assert store.ACTION_APPROVE in actions
    approves = store.list_audit(action=store.ACTION_APPROVE)
    assert approves and all(e["action"] == "approve" for e in approves)


def test_audit_dataset_scoping():
    req = make_request(username="ned", target_type="dataset", target_id=51)
    store.review_request(req["id"], True, "steward1")
    scoped = store.list_audit(dataset_ids=[51])
    assert scoped and all(e["dataset_id"] == 51 for e in scoped)
    # an empty dataset filter returns nothing (steward with no datasets)
    assert store.list_audit(dataset_ids=[]) == []


def test_legacy_json_is_migrated(gate_dir):
    import json as _json
    rid = "a" * 32
    reqs = gate_dir / "requests"
    reqs.mkdir(parents=True, exist_ok=True)
    (reqs / (rid + ".json")).write_text(_json.dumps({
        "id": rid, "username": "old", "user_id": 1,
        "target_type": "dataset", "target_id": 33, "reason": "legacy",
        "documents": [], "status": "approved",
        "created_at": "2020-01-01T00:00:00+00:00",
        "reviewed_by": "root", "expires_at": None,
    }))
    # force _connect to re-run schema + legacy migration for this tmp db
    store._INITIALIZED.clear()
    assert store.get_request(rid)["username"] == "old"
    # an approved legacy request seeds an equivalent covering grant
    assert store.has_approval("old", 1, [33])


# --- WS-H2: X-Accel-Redirect path safety ----------------------------------

def test_xaccel_uri_basic():
    assert store.xaccel_internal_uri(
        "/OMERO/ManagedRepository", "/_protected",
        "user_1/2020-01/Fileset_1", "slide.svs"
    ) == "/_protected/user_1/2020-01/Fileset_1/slide.svs"


def test_xaccel_uri_encodes_spaces_and_normalises_prefix():
    # a prefix without a leading slash still yields a rooted URI
    assert store.xaccel_internal_uri(
        "/OMERO/ManagedRepository", "_protected", "a b", "s d.svs"
    ) == "/_protected/a%20b/s%20d.svs"


def test_xaccel_uri_rejects_dotdot_traversal():
    assert store.xaccel_internal_uri(
        "/OMERO/MR", "/_protected", "../../etc", "passwd") is None
    assert store.xaccel_internal_uri(
        "/OMERO/MR", "/_protected", "..", "..") is None


def test_xaccel_uri_rejects_absolute_injection():
    # an absolute path or name would join-escape the managed root
    assert store.xaccel_internal_uri(
        "/OMERO/MR", "/_protected", "/etc", "passwd") is None
    assert store.xaccel_internal_uri(
        "/OMERO/MR", "/_protected", "sub", "/etc/passwd") is None


def test_xaccel_uri_rejects_root_only_and_misconfig():
    # resolves to the root dir itself (no file) -> None
    assert store.xaccel_internal_uri(
        "/OMERO/MR", "/_protected", "", "") is None
    # refuse filesystem root as the managed root
    assert store.xaccel_internal_uri("/", "/_protected", "x", "y") is None
    # empty internal prefix is a misconfiguration -> None
    assert store.xaccel_internal_uri(
        "/OMERO/MR", "", "a", "b.svs") is None


# --------------------------------------------------------------------------
# Direct-read path (no NGINX): same traversal guard as X-Accel
# --------------------------------------------------------------------------
def test_managed_file_path_basic():
    assert store.managed_file_path(
        "/OMERO/ManagedRepository", "Admin_52/2026-10/04/15-47-28.257/",
        "REF00.png"
    ) == "/OMERO/ManagedRepository/Admin_52/2026-10/04/15-47-28.257/REF00.png"


def test_managed_file_path_keeps_spaces_unencoded():
    # a filesystem path, not a URI: no percent-encoding
    assert store.managed_file_path(
        "/OMERO/MR/", "a b", "s d.svs") == "/OMERO/MR/a b/s d.svs"


def test_managed_file_path_rejects_traversal_and_absolute_injection():
    assert store.managed_file_path("/OMERO/MR", "../../etc", "passwd") is None
    assert store.managed_file_path("/OMERO/MR", "/etc", "passwd") is None
    assert store.managed_file_path("/OMERO/MR", "sub", "/etc/passwd") is None


def test_managed_file_path_rejects_root_only_and_misconfig():
    assert store.managed_file_path("/OMERO/MR", "", "") is None
    assert store.managed_file_path("/", "x", "y") is None
    assert store.managed_file_path("", "x", "y") is None


# --------------------------------------------------------------------------
# Container-level grant coverage (browsing an approved dataset / project)
# --------------------------------------------------------------------------
def test_scope_approval_dataset_grant_covers_only_that_dataset():
    store.create_grant("alice", store.SCOPE_DATASET, 12, "root")
    assert store.has_scope_approval("alice", dataset_ids=[12])
    assert not store.has_scope_approval("alice", dataset_ids=[13])
    assert not store.has_scope_approval("bob", dataset_ids=[12])


def test_scope_approval_project_grant_covers_child_datasets():
    store.create_grant("alice", store.SCOPE_PROJECT, 7, "root")
    # a dataset whose parent project is 7 is covered by the project grant
    assert store.has_scope_approval("alice", dataset_ids=[12],
                                    project_ids=[7])
    assert store.has_scope_approval("alice", project_ids=[7])
    assert not store.has_scope_approval("alice", dataset_ids=[12],
                                        project_ids=[8])


def test_scope_approval_ignores_image_revoked_and_expired_grants():
    # an image grant never covers a whole container
    store.create_grant("alice", store.SCOPE_IMAGE, 12, "root")
    assert not store.has_scope_approval("alice", dataset_ids=[12])
    revoked = store.create_grant("alice", store.SCOPE_DATASET, 20, "root")
    store.revoke_grant(revoked["id"], "root")
    assert not store.has_scope_approval("alice", dataset_ids=[20])
    store.create_grant("alice", store.SCOPE_DATASET, 21, "root",
                       expires_at="2000-01-01T00:00:00+00:00")
    assert not store.has_scope_approval("alice", dataset_ids=[21])


def test_scope_approval_via_approved_dataset_request():
    # the real path: request -> approve (default scope) -> browsable
    req = make_request(username="dana", target_id=55)
    assert not store.has_scope_approval("dana", dataset_ids=[55])
    store.review_request(req["id"], True, "steward1")
    assert store.has_scope_approval("dana", dataset_ids=[55])


# --------------------------------------------------------------------------
# Requester-side access states (request-form dataset picker)
# --------------------------------------------------------------------------
def _ds(did, owner_id=1, project_ids=()):
    return {"id": did, "name": "SD%04d" % did, "owner_id": owner_id,
            "project_ids": list(project_ids)}


def test_access_states_cover_all_four_states():
    grants = [
        {"scope_type": store.SCOPE_DATASET, "scope_id": 2, "active": True},
        {"scope_type": store.SCOPE_PROJECT, "scope_id": 9, "active": True},
    ]
    pending = [{"target_type": store.TARGET_DATASET, "target_id": 4}]
    out = store.dataset_access_states(
        [_ds(1, owner_id=5), _ds(2), _ds(3, project_ids=[9]), _ds(4),
         _ds(6)],
        user_id=5, grants=grants, pending_requests=pending)
    assert [d["access"] for d in out] == [
        store.ACCESS_OWNER,        # owned by the requester
        store.ACCESS_GRANTED,      # dataset grant
        store.ACCESS_GRANTED,      # covered by its project's grant
        store.ACCESS_PENDING,      # request still in review
        store.ACCESS_REQUESTABLE,  # nothing yet
    ]


def test_access_states_precedence_and_inactive_grants():
    # owning beats a grant; a grant beats a pending request
    grants = [{"scope_type": store.SCOPE_DATASET, "scope_id": 1,
               "active": True},
              {"scope_type": store.SCOPE_DATASET, "scope_id": 2,
               "active": True},
              {"scope_type": store.SCOPE_DATASET, "scope_id": 3,
               "active": False}]
    pending = [{"target_type": store.TARGET_DATASET, "target_id": 2},
               {"target_type": store.TARGET_IMAGE, "target_id": 3}]
    out = store.dataset_access_states(
        [_ds(1, owner_id=5), _ds(2), _ds(3)], user_id=5, grants=grants,
        pending_requests=pending)
    assert out[0]["access"] == store.ACCESS_OWNER
    assert out[1]["access"] == store.ACCESS_GRANTED
    # an expired/revoked grant and an *image* request don't count
    assert out[2]["access"] == store.ACCESS_REQUESTABLE


def test_merge_rows_collapses_links_and_sources():
    rows = [
        (1, "SD0001", 3, 7, "CRM Dataset"),
        (1, "SD0001", 3, 8, "Teaching set"),   # second project link
        (2, "orphan", 3, None, None),          # in no project
        (1, "SD0001", 3, 7, "CRM Dataset"),    # same row from 2nd source
    ]
    out = {d["id"]: d for d in store.merge_dataset_rows(rows)}
    assert set(out) == {1, 2}
    assert out[1]["project_ids"] == [7, 8]
    assert (out[1]["project_id"], out[1]["project_name"]) == \
        (7, "CRM Dataset")
    assert out[2]["project_ids"] == [] and out[2]["project_name"] is None


def test_merge_rows_names_unnamed_and_feeds_access_states():
    merged = store.merge_dataset_rows([(4, None, 9, 7, "CRM Dataset")])
    assert merged[0]["name"] == "Unnamed dataset"
    grants = [{"scope_type": store.SCOPE_PROJECT, "scope_id": 7,
               "active": True}]
    out = store.dataset_access_states(merged, user_id=5, grants=grants,
                                      pending_requests=[])
    assert out[0]["access"] == store.ACCESS_GRANTED


def test_access_states_returns_copies():
    src = [_ds(1)]
    out = store.dataset_access_states(src, user_id=None, grants=[],
                                      pending_requests=[])
    assert "access" not in src[0]
    assert out[0]["access"] == store.ACCESS_REQUESTABLE
