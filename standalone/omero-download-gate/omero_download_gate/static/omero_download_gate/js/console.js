/* =========================================================================
   WS-I Data-Access Governance Console: logic
   CSP-safe: loaded via <script src>, addEventListener only, no inline on*.
   All user-supplied strings enter the DOM via textContent (never innerHTML),
   so raw user text is never interpreted as markup.
   ========================================================================= */
(function () {
  "use strict";

  // ---- config (emitted server-side as JSON; data, not executable code) ----
  var cfg = {};
  try {
    cfg = JSON.parse(document.getElementById("gate-config").textContent);
  } catch (e) {
    cfg = {};
  }
  var docTypes = [];
  try {
    docTypes = JSON.parse(document.getElementById("gate-doctypes").textContent);
  } catch (e) {
    docTypes = ["ethics", "proposal", "dua"];
  }

  var CSRF = (function () {
    var el = document.querySelector("input[name='csrfmiddlewaretoken']");
    return el ? el.value : "";
  })();

  var indexPath = (cfg.index || "/").replace(/\/$/, "");
  var docUrlBase = indexPath + "/doc/";
  function policyUrlFor(id) {
    return indexPath + "/policy/dataset/" + encodeURIComponent(id) + "/";
  }

  var AUDIT_ACTIONS = ["request", "approve", "deny", "revoke",
                       "download", "policy_set"];

  // ---------------------------------------------------------------------
  // Small DOM builder: attrs.text sets textContent (safe for user data);
  // attrs.html is used ONLY with trusted constant SVG below.
  // ---------------------------------------------------------------------
  function h(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "text") { node.textContent = v; }
        else if (k === "html") { node.innerHTML = v; }
        else if (k === "class") { node.className = v; }
        else if (k === "dataset") {
          Object.keys(v).forEach(function (dk) { node.dataset[dk] = v[dk]; });
        }
        else if (k.indexOf("aria-") === 0 || k.indexOf("data-") === 0 ||
                 k === "role" || k === "for" || k === "type" || k === "name" ||
                 k === "value" || k === "min" || k === "placeholder" ||
                 k === "href" || k === "tabindex" || k === "id" ||
                 k === "checked" || k === "hidden" || k === "download") {
          if (k === "checked") { node.checked = !!v; }
          else if (k === "hidden") { node.hidden = !!v; }
          else { node.setAttribute(k, v); }
        }
        else { node.setAttribute(k, v); }
      });
    }
    appendChildren(node, children);
    return node;
  }
  function appendChildren(node, children) {
    if (children === null || children === undefined) return;
    if (!Array.isArray(children)) children = [children];
    children.forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      if (typeof c === "string" || typeof c === "number") {
        node.appendChild(document.createTextNode(String(c)));
      } else {
        node.appendChild(c);
      }
    });
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function byId(id) { return document.getElementById(id); }

  // trusted constant inline SVG icons (no user data)
  var ICON = {
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>',
    download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m0 0 4-4m-4 4-4-4M5 21h14"/></svg>',
    refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"/></svg>'
  };

  // ---------------------------------------------------------------------
  // Formatting / status mapping
  // ---------------------------------------------------------------------
  function fmtDate(iso) {
    if (!iso) return "-";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString();
  }
  function pillFor(label, variant) {
    return h("span", { class: "pill pill--" + variant, text: label });
  }
  function requestPill(status) {
    var v = status === "approved" ? "ok"
          : status === "denied" ? "bad" : "pending";
    return pillFor(status, v);
  }
  function grantPill(g) {
    if (g.revoked) return pillFor("revoked", "bad");
    if (!g.active) return pillFor("expired", "bad");
    return pillFor("active", "ok");
  }
  function actionPill(action) {
    var v = action === "approve" ? "ok"
          : (action === "deny" || action === "revoke") ? "bad"
          : action === "request" ? "pending" : "neutral";
    return h("span", { class: "pill" + (v === "neutral" ? "" : " pill--" + v),
                       text: action });
  }
  function scopeText(type, id) {
    return (type || "?") + " #" + (id === null || id === undefined ? "?" : id);
  }

  // ---------------------------------------------------------------------
  // HTTP helpers
  // ---------------------------------------------------------------------
  function getJson(url) {
    return fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (res) {
        return res.json().then(function (data) {
          if (!res.ok || data.success === false) {
            throw new Error((data.error && data.error.message) ||
                            ("Request failed (" + res.status + ")"));
          }
          return data;
        });
      });
  }
  function postForm(url, params) {
    var fd = new FormData();
    fd.append("csrfmiddlewaretoken", CSRF);
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v !== null && v !== undefined && v !== "") fd.append(k, v);
    });
    return fetch(url, {
      method: "POST",
      body: fd,
      headers: { "X-CSRFToken": CSRF, "X-Requested-With": "XMLHttpRequest" }
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok || data.success === false) {
          throw new Error((data.error && data.error.message) ||
                          ("Action failed (" + res.status + ")"));
        }
        return data;
      });
    });
  }

  // ---------------------------------------------------------------------
  // Toast
  // ---------------------------------------------------------------------
  var toastWrap = null;
  function toast(message, kind) {
    if (!toastWrap) {
      toastWrap = h("div", { class: "toast-wrap", "aria-live": "polite",
                             "aria-atomic": "true" });
      document.body.appendChild(toastWrap);
    }
    var t = h("div", { class: "toast is-" + (kind || "ok"), role: "status",
                       text: message });
    toastWrap.appendChild(t);
    setTimeout(function () {
      if (t.parentNode) t.parentNode.removeChild(t);
    }, 4000);
  }

  // ---------------------------------------------------------------------
  // Focus trap (shared by drawer + modal)
  // ---------------------------------------------------------------------
  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]),' +
                  ' select:not([disabled]), textarea:not([disabled]),' +
                  ' [tabindex]:not([tabindex="-1"])';
  function focusables(container) {
    return Array.prototype.filter.call(
      container.querySelectorAll(FOCUSABLE),
      function (el) { return el.offsetParent !== null || el === document.activeElement; });
  }
  function trapTab(container, e) {
    if (e.key !== "Tab") return;
    var list = focusables(container);
    if (!list.length) return;
    var first = list[0], last = list[list.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  }

  // =====================================================================
  // Tabs
  // =====================================================================
  var tabButtons = [];
  var panels = {};
  function initTabs() {
    var tablist = byId("tablist");
    var underline = byId("tab-underline");
    tabButtons = Array.prototype.slice.call(tablist.querySelectorAll(".tab"));

    tabButtons.forEach(function (btn) {
      panels[btn.getAttribute("aria-controls")] =
        byId(btn.getAttribute("aria-controls"));
      btn.addEventListener("click", function () { selectTab(btn); });
      btn.addEventListener("keydown", function (e) {
        var i = tabButtons.indexOf(btn);
        if (e.key === "ArrowRight" || e.key === "ArrowDown") {
          e.preventDefault();
          tabButtons[(i + 1) % tabButtons.length].focus();
          selectTab(tabButtons[(i + 1) % tabButtons.length]);
        } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
          e.preventDefault();
          var p = (i - 1 + tabButtons.length) % tabButtons.length;
          tabButtons[p].focus();
          selectTab(tabButtons[p]);
        } else if (e.key === "Home") {
          e.preventDefault(); tabButtons[0].focus(); selectTab(tabButtons[0]);
        } else if (e.key === "End") {
          e.preventDefault();
          tabButtons[tabButtons.length - 1].focus();
          selectTab(tabButtons[tabButtons.length - 1]);
        }
      });
    });

    function moveUnderline(btn) {
      underline.style.width = btn.offsetWidth + "px";
      underline.style.transform = "translateX(" + btn.offsetLeft + "px)";
    }

    window.selectTab = function (btn) {
      tabButtons.forEach(function (b) {
        var on = b === btn;
        b.setAttribute("aria-selected", on ? "true" : "false");
        b.setAttribute("tabindex", on ? "0" : "-1");
        var panel = panels[b.getAttribute("aria-controls")];
        if (panel) panel.hidden = !on;
      });
      moveUnderline(btn);
      var loader = TAB_LOADERS[btn.getAttribute("aria-controls")];
      if (loader) loader();
    };

    // internal alias used above
    function selectTab(btn) { window.selectTab(btn); }

    window.addEventListener("resize", function () {
      var active = tablist.querySelector('.tab[aria-selected="true"]');
      if (active) moveUnderline(active);
    });

    // activate the first tab
    selectTab(tabButtons[0]);
  }

  var loadedOnce = {};
  var TAB_LOADERS = {
    "panel-requests": function () { loadRequests(); },
    "panel-grants": function () {
      if (!loadedOnce.grants) { loadedOnce.grants = true; loadGrants(); }
    },
    "panel-policies": function () { /* loads on demand via form */ },
    "panel-audit": function () {
      if (!loadedOnce.audit) { loadedOnce.audit = true; loadAudit(); }
    }
  };

  // =====================================================================
  // Requests tab
  // =====================================================================
  function loadRequests() {
    var pendingBox = byId("pending-list");
    var reviewedBox = byId("reviewed-list");
    pendingBox.textContent = ""; reviewedBox.textContent = "";
    pendingBox.appendChild(h("div", { class: "loading", text: "Loading requests..." }));
    getJson(cfg.reviewList).then(function (data) {
      renderReqList(pendingBox, data.pending || [], true, "Nothing pending.");
      renderReqList(reviewedBox, data.reviewed || [], false, "Nothing reviewed yet.");
    }).catch(function (err) {
      clear(pendingBox);
      pendingBox.appendChild(h("div", { class: "empty", text: err.message }));
    });
  }

  function renderReqList(box, items, pending, emptyMsg) {
    clear(box);
    if (!items.length) {
      box.appendChild(h("div", { class: "empty", text: emptyMsg }));
      return;
    }
    var list = h("div", { class: "req-list" });
    items.forEach(function (req) {
      var docCount = (req.documents || []).length;
      var row = h("button", {
        class: "req-row", type: "button",
        "aria-label": "Open request from " + (req.username || "user") +
                      " for " + scopeText(req.target_type, req.target_id)
      }, [
        requestPill(req.status),
        h("span", { class: "req-who", text: req.username || "-" }),
        h("span", { class: "req-target", text: scopeText(req.target_type, req.target_id) }),
        docCount ? h("span", { class: "doc-count",
                               text: docCount + (docCount === 1 ? " doc" : " docs") }) : null,
        h("span", { class: "req-when", text: fmtDate(req.created_at) })
      ]);
      row.addEventListener("click", function () { openDrawer(req, pending, row); });
      list.appendChild(row);
    });
    box.appendChild(list);
  }

  // ---- Detail drawer --------------------------------------------------
  var drawer, drawerBackdrop, drawerReturnFocus = null, drawerKeydown = null;

  function ensureDrawer() {
    drawerBackdrop = byId("drawer-backdrop");
    drawer = byId("drawer");
    byId("drawer-close").addEventListener("click", closeDrawer);
    drawerBackdrop.addEventListener("click", closeDrawer);
  }

  function openDrawer(req, pending, sourceEl) {
    drawerReturnFocus = sourceEl || document.activeElement;
    var body = byId("drawer-body");
    clear(body);
    byId("drawer-title").textContent =
      (req.username || "Request") + ", " + scopeText(req.target_type, req.target_id);

    // meta line
    body.appendChild(h("div", { class: "drawer-meta" }, [
      requestPill(req.status),
      "  requested " + fmtDate(req.created_at)
    ]));

    // reason
    if (req.reason) {
      body.appendChild(fieldBlock("Reason",
        h("p", { text: req.reason, class: "muted" })));
    }

    // typed documents
    var docs = req.documents_detail && req.documents_detail.length
      ? req.documents_detail
      : (req.documents || []).map(function (f) { return { filename: f, type: null }; });
    var docWrap;
    if (docs.length) {
      docWrap = h("div", {});
      docs.forEach(function (d) {
        var url = docUrlBase + req.id + "/" + encodeURIComponent(d.filename);
        docWrap.appendChild(h("div", { class: "doc-item" }, [
          d.type ? h("span", { class: "doc-type-badge", text: d.type }) : null,
          h("a", { class: "grow-link", href: url, download: "",
                   text: d.filename })
        ]));
      });
    } else {
      docWrap = h("p", { class: "muted", text: "No supporting documents attached." });
    }
    body.appendChild(fieldBlock("Documents", docWrap));

    if (pending && req.status === "pending") {
      body.appendChild(buildReviewControls(req));
    } else {
      body.appendChild(buildOutcome(req));
    }

    drawer.setAttribute("data-open", "true");
    drawerBackdrop.setAttribute("data-open", "true");
    drawerKeydown = function (e) {
      if (e.key === "Escape") { closeDrawer(); return; }
      trapTab(drawer, e);
    };
    document.addEventListener("keydown", drawerKeydown);
    byId("drawer-close").focus();
  }

  function closeDrawer() {
    if (!drawer) return;
    drawer.setAttribute("data-open", "false");
    drawerBackdrop.setAttribute("data-open", "false");
    if (drawerKeydown) {
      document.removeEventListener("keydown", drawerKeydown);
      drawerKeydown = null;
    }
    if (drawerReturnFocus && drawerReturnFocus.focus) {
      drawerReturnFocus.focus();
    }
  }

  function fieldBlock(label, node) {
    return h("div", { class: "field-block" }, [
      h("div", { class: "section-label", text: label }),
      node
    ]);
  }

  function buildOutcome(req) {
    var rows = [];
    rows.push(h("div", { class: "drawer-meta",
      text: "Reviewed by " + (req.reviewed_by || "?") + ", " + fmtDate(req.reviewed_at) }));
    if (req.review_note) {
      rows.push(h("p", { class: "muted", text: "Note: " + req.review_note }));
    }
    rows.push(h("p", { class: "muted",
      text: "Access expiry: " + (req.expires_at ? fmtDate(req.expires_at) : "standing / none") }));
    return fieldBlock("Outcome", h("div", {}, rows));
  }

  function buildReviewControls(req) {
    var wrap = h("div", { class: "field-block" });
    wrap.appendChild(h("div", { class: "section-label", text: "Grant scope" }));

    var scopeName = "scope-" + req.id;
    var defaultScope = req.target_type === "dataset" ? "dataset" : "image";
    var scopeGroup = h("div", { class: "choice-group", role: "radiogroup",
                                "aria-label": "Grant scope" });
    [["image", "This image"], ["dataset", "Parent dataset"],
     ["project", "Grandparent project"]].forEach(function (opt) {
      var input = h("input", { type: "radio", name: scopeName, value: opt[0],
                               checked: opt[0] === defaultScope });
      scopeGroup.appendChild(h("label", { class: "choice" }, [input, opt[1]]));
    });
    wrap.appendChild(scopeGroup);
    wrap.appendChild(h("div", { class: "inline-field" }, [
      h("label", { class: "muted", for: "scopeid-" + req.id,
                   text: "Scope ID (optional):" }),
      h("input", { type: "number", min: "1", id: "scopeid-" + req.id,
                   placeholder: "auto from lineage" })
    ]));

    // expiry
    wrap.appendChild(h("div", { class: "section-label", text: "Expiry" }));
    var expName = "exp-" + req.id;
    var expGroup = h("div", { class: "choice-group", role: "radiogroup",
                              "aria-label": "Expiry" });
    [["default", "Policy default"], ["standing", "Standing (never)"],
     ["days", "In N days"], ["date", "On date"]].forEach(function (opt) {
      var input = h("input", { type: "radio", name: expName, value: opt[0],
                               checked: opt[0] === "default" });
      expGroup.appendChild(h("label", { class: "choice" }, [input, opt[1]]));
    });
    wrap.appendChild(expGroup);

    var daysInput = h("input", { type: "number", min: "1",
                                 placeholder: "days", id: "expdays-" + req.id });
    var dateInput = h("input", { type: "date", id: "expdate-" + req.id });
    var daysWrap = h("div", { class: "inline-field hidden" },
      [h("label", { class: "muted", for: "expdays-" + req.id, text: "Days:" }), daysInput]);
    var dateWrap = h("div", { class: "inline-field hidden" },
      [h("label", { class: "muted", for: "expdate-" + req.id, text: "Date:" }), dateInput]);
    wrap.appendChild(daysWrap);
    wrap.appendChild(dateWrap);
    expGroup.addEventListener("change", function () {
      var v = expGroup.querySelector("input:checked").value;
      daysWrap.classList.toggle("hidden", v !== "days");
      dateWrap.classList.toggle("hidden", v !== "date");
    });

    // note
    wrap.appendChild(h("div", { class: "section-label", text: "Note to requester" }));
    var note = h("textarea", { placeholder: "Optional note...", id: "note-" + req.id });
    wrap.appendChild(note);

    // actions
    var approveBtn = h("button", { class: "btn btn-primary", type: "button",
                                   text: "Approve" });
    var denyBtn = h("button", { class: "btn btn-danger", type: "button",
                                text: "Deny" });
    approveBtn.addEventListener("click", function () { submitAction(req, "approve", scopeName, expName, approveBtn, denyBtn); });
    denyBtn.addEventListener("click", function () { submitAction(req, "deny", scopeName, expName, approveBtn, denyBtn); });
    wrap.appendChild(h("div", { class: "btn-row", style: "margin-top:16px" },
      [approveBtn, denyBtn]));
    return wrap;
  }

  function submitAction(req, action, scopeName, expName, approveBtn, denyBtn) {
    var params = { request_id: req.id, action: action };
    params.note = (byId("note-" + req.id) || {}).value || "";

    if (action === "approve") {
      var scope = drawer.querySelector("input[name='" + scopeName + "']:checked");
      if (scope) params.scope = scope.value;
      var sid = (byId("scopeid-" + req.id) || {}).value;
      if (sid) params.scope_id = sid;

      var exp = drawer.querySelector("input[name='" + expName + "']:checked");
      var expVal = exp ? exp.value : "default";
      if (expVal === "standing") { params.standing = "1"; }
      else if (expVal === "days") {
        var d = (byId("expdays-" + req.id) || {}).value;
        if (d) params.expires_days = d;
      } else if (expVal === "date") {
        var dt = (byId("expdate-" + req.id) || {}).value;
        if (dt) params.expires_at = dt;
      }
      // "default": send none; the server applies the policy's default expiry.
    }

    approveBtn.disabled = true; denyBtn.disabled = true;
    postForm(cfg.reviewAction, params).then(function () {
      closeDrawer();
      loadRequests();
      toast(action === "approve" ? "Request approved." : "Request denied.",
            action === "approve" ? "ok" : "error");
    }).catch(function (err) {
      approveBtn.disabled = false; denyBtn.disabled = false;
      toast(err.message, "error");
    });
  }


  // =====================================================================
  // Grants tab
  // =====================================================================
  function loadGrants() {
    var box = byId("grants-table-wrap");
    clear(box);
    box.appendChild(h("div", { class: "loading", text: "Loading grants..." }));
    var principal = (byId("grants-principal") || {}).value || "";
    var activeOnly = byId("grants-active-only") && byId("grants-active-only").checked;
    var url = cfg.grants + "?active=" + (activeOnly ? "1" : "0") +
      (principal ? "&principal=" + encodeURIComponent(principal) : "");
    getJson(url).then(function (data) {
      renderGrants(box, data.grants || []);
    }).catch(function (err) {
      clear(box);
      box.appendChild(h("div", { class: "empty", text: err.message }));
    });
  }

  function renderGrants(box, grants) {
    clear(box);
    if (!grants.length) {
      box.appendChild(h("div", { class: "empty", text: "No grants match." }));
      return;
    }
    var thead = h("thead", {}, h("tr", {}, [
      cell("th", "Principal"), cell("th", "Scope"), cell("th", "Granted by"),
      cell("th", "Granted"), cell("th", "Expiry"), cell("th", "Status"),
      h("th", { class: "col-actions", text: "" })
    ]));
    var tbody = h("tbody", {});
    grants.forEach(function (g) {
      var actionsTd = h("td", { class: "col-actions" });
      if (g.active) {
        var rev = h("button", { class: "btn btn-secondary", type: "button",
          text: "Revoke",
          "aria-label": "Revoke grant for " + (g.principal || "principal") });
        rev.addEventListener("click", function () { openRevoke(g); });
        actionsTd.appendChild(rev);
      } else {
        actionsTd.appendChild(h("span", { class: "faint mono", text: "-" }));
      }
      tbody.appendChild(h("tr", {}, [
        cell("td", g.principal || "-"),
        h("td", { class: "mono", text: scopeText(g.scope_type, g.scope_id) }),
        cell("td", g.granted_by || "-"),
        h("td", { class: "mono", text: fmtDate(g.granted_at) }),
        h("td", { class: "mono", text: g.expires_at ? fmtDate(g.expires_at) : "standing" }),
        h("td", {}, grantPill(g)),
        actionsTd
      ]));
    });
    box.appendChild(h("table", { class: "data-table" }, [thead, tbody]));
  }
  function cell(tag, txt) { return h(tag, { text: txt }); }

  // ---- revoke modal ---------------------------------------------------
  var modal, modalBackdrop, modalReturnFocus = null, modalKeydown = null, pendingGrant = null;
  function ensureModal() {
    modalBackdrop = byId("modal-backdrop");
    modal = byId("modal");
    byId("modal-cancel").addEventListener("click", closeModal);
    byId("modal-confirm").addEventListener("click", confirmRevoke);
    modalBackdrop.addEventListener("click", function (e) {
      if (e.target === modalBackdrop) closeModal();
    });
  }
  function openRevoke(g) {
    pendingGrant = g;
    modalReturnFocus = document.activeElement;
    byId("modal-title").textContent = "Revoke grant";
    byId("modal-desc").textContent =
      (g.principal || "principal") + ", " + scopeText(g.scope_type, g.scope_id);
    byId("modal-reason").value = "";
    modalBackdrop.setAttribute("data-open", "true");
    modalKeydown = function (e) {
      if (e.key === "Escape") { closeModal(); return; }
      trapTab(modal, e);
    };
    document.addEventListener("keydown", modalKeydown);
    byId("modal-reason").focus();
  }
  function closeModal() {
    modalBackdrop.setAttribute("data-open", "false");
    if (modalKeydown) {
      document.removeEventListener("keydown", modalKeydown);
      modalKeydown = null;
    }
    if (modalReturnFocus && modalReturnFocus.focus) modalReturnFocus.focus();
    pendingGrant = null;
  }
  function confirmRevoke() {
    if (!pendingGrant) return;
    var reason = byId("modal-reason").value || "";
    var btn = byId("modal-confirm");
    btn.disabled = true;
    postForm(cfg.grantRevoke, { grant_id: pendingGrant.id, reason: reason })
      .then(function () {
        btn.disabled = false;
        closeModal();
        toast("Grant revoked.", "error");
        loadGrants();
      }).catch(function (err) {
        btn.disabled = false;
        toast(err.message, "error");
      });
  }

  // =====================================================================
  // Policies tab
  // =====================================================================
  var currentPolicyDataset = null;
  function initPolicies() {
    byId("policy-load").addEventListener("click", function () {
      var id = (byId("policy-dataset-id") || {}).value;
      if (!id) { toast("Enter a dataset ID.", "error"); return; }
      loadPolicy(id);
    });
    byId("policy-save").addEventListener("click", savePolicy);
    // build required-docs checkboxes from server-provided doc types
    var box = byId("policy-required-docs");
    clear(box);
    docTypes.forEach(function (t) {
      var input = h("input", { type: "checkbox", value: t,
                               id: "reqdoc-" + t, "data-doc": t });
      box.appendChild(h("label", { class: "choice" }, [input, t]));
    });
  }

  function loadPolicy(id) {
    var status = byId("policy-status");
    status.textContent = "Loading policy for dataset #" + id + "...";
    getJson(policyUrlFor(id)).then(function (data) {
      currentPolicyDataset = data.dataset_id;
      byId("policy-form").hidden = false;
      var p = data.policy;

      // default approvers info
      var da = byId("policy-default-approvers");
      clear(da);
      da.appendChild(h("span", {
        text: (data.default_approvers && data.default_approvers.length)
          ? "Fallback approvers: " + data.default_approvers.join(", ")
          : "No default approvers configured, so only admins can approve." }));

      byId("policy-approvers").value =
        p && p.approver_principals ? p.approver_principals.join(", ") : "";
      byId("policy-expiry-days").value =
        p && p.default_expiry_days ? p.default_expiry_days : "";
      byId("policy-auto-expire").checked = p ? !!p.auto_expire : true;

      var required = (p && p.required_docs) ? p.required_docs : [];
      Array.prototype.forEach.call(
        byId("policy-required-docs").querySelectorAll("input[type=checkbox]"),
        function (cb) { cb.checked = required.indexOf(cb.value) !== -1; });

      status.textContent = p
        ? "Editing policy for dataset #" + data.dataset_id +
          " (updated " + fmtDate(p.updated_at) + " by " + (p.updated_by || "?") + ")"
        : "No policy yet for dataset #" + data.dataset_id + ". Create one below.";
    }).catch(function (err) {
      byId("policy-form").hidden = true;
      status.textContent = err.message;
    });
  }

  function savePolicy() {
    if (currentPolicyDataset === null) return;
    var params = {
      approver_principals: byId("policy-approvers").value || "",
      default_expiry_days: byId("policy-expiry-days").value || "",
      auto_expire: byId("policy-auto-expire").checked ? "1" : "0"
    };
    // required_docs: send each checked type as a repeated `required_doc`
    var checked = Array.prototype.filter.call(
      byId("policy-required-docs").querySelectorAll("input[type=checkbox]"),
      function (cb) { return cb.checked; }).map(function (cb) { return cb.value; });
    // FormData needs repeated appends and postForm only does single ones, so build it here.
    var fd = new FormData();
    fd.append("csrfmiddlewaretoken", CSRF);
    fd.append("approver_principals", params.approver_principals);
    fd.append("default_expiry_days", params.default_expiry_days);
    fd.append("auto_expire", params.auto_expire);
    checked.forEach(function (v) { fd.append("required_doc", v); });

    var btn = byId("policy-save");
    btn.disabled = true;
    fetch(policyUrlFor(currentPolicyDataset), {
      method: "POST", body: fd,
      headers: { "X-CSRFToken": CSRF, "X-Requested-With": "XMLHttpRequest" }
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok || data.success === false) {
          throw new Error((data.error && data.error.message) || "Save failed");
        }
        return data;
      });
    }).then(function () {
      btn.disabled = false;
      toast("Policy saved for dataset #" + currentPolicyDataset + ".", "ok");
      loadPolicy(currentPolicyDataset);
    }).catch(function (err) {
      btn.disabled = false;
      toast(err.message, "error");
    });
  }

  // =====================================================================
  // Audit tab
  // =====================================================================
  function initAudit() {
    var sel = byId("audit-action");
    AUDIT_ACTIONS.forEach(function (a) {
      sel.appendChild(h("option", { value: a, text: a }));
    });
    byId("audit-load").addEventListener("click", function () {
      loadedOnce.audit = true; loadAudit();
    });
  }

  function loadAudit() {
    var box = byId("audit-table-wrap");
    clear(box);
    box.appendChild(h("div", { class: "loading", text: "Loading audit log..." }));
    var action = (byId("audit-action") || {}).value || "";
    var limit = (byId("audit-limit") || {}).value || "";
    var url = cfg.audit + "?limit=" + encodeURIComponent(limit || "200") +
      (action ? "&action=" + encodeURIComponent(action) : "");
    getJson(url).then(function (data) {
      renderAudit(box, data.events || []);
    }).catch(function (err) {
      clear(box);
      box.appendChild(h("div", { class: "empty", text: err.message }));
    });
  }

  function renderAudit(box, events) {
    clear(box);
    if (!events.length) {
      box.appendChild(h("div", { class: "empty", text: "No audit events match." }));
      return;
    }
    var thead = h("thead", {}, h("tr", {}, [
      cell("th", "Actor"), cell("th", "Action"), cell("th", "Target"),
      cell("th", "Dataset"), cell("th", "Detail"), cell("th", "Timestamp")
    ]));
    var tbody = h("tbody", {});
    events.forEach(function (ev) {
      tbody.appendChild(h("tr", {}, [
        cell("td", ev.actor || "-"),
        h("td", {}, actionPill(ev.action)),
        cell("td", ev.target_type
          ? scopeText(ev.target_type, ev.target_id) : (ev.target_id || "-")),
        cell("td", ev.dataset_id === null || ev.dataset_id === undefined
          ? "-" : "#" + ev.dataset_id),
        h("td", { class: "detail", text: ev.detail || "" }),
        cell("td", fmtDate(ev.ts))
      ]));
    });
    box.appendChild(h("table", { class: "data-table audit-table" }, [thead, tbody]));
  }


  // =====================================================================
  // Init
  // =====================================================================
  function init() {
    ensureDrawer();
    ensureModal();
    initTabs();
    initPolicies();
    initAudit();

    // Grants filters (principal + active-only) re-query on demand.
    var grantsApply = byId("grants-apply");
    if (grantsApply) grantsApply.addEventListener("click", loadGrants);
    var grantsActive = byId("grants-active-only");
    if (grantsActive) grantsActive.addEventListener("change", loadGrants);

    var refresh = byId("refresh-btn");
    if (refresh) {
      refresh.addEventListener("click", function () {
        var active = document.querySelector('.tab[aria-selected="true"]');
        var id = active ? active.getAttribute("aria-controls") : "panel-requests";
        if (id === "panel-requests") loadRequests();
        else if (id === "panel-grants") loadGrants();
        else if (id === "panel-audit") loadAudit();
        else if (id === "panel-policies" && currentPolicyDataset !== null)
          loadPolicy(currentPolicyDataset);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
