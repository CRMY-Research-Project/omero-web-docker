/* =========================================================================
   Download gate - requester page (index.html)

   Three panels over the gate's JSON endpoints:
     1. New request - a searchable dataset picker (ARIA combobox) fed by
        datasets/requestable/, a numeric-id fallback, typed documents.
     2. My access   - the requester's live grants (access/mine/), each
        expandable into datasets / images via access/<scope>/<id>/, with
        download links. Deep link: #grant-<id> opens that grant.
     3. My requests - request history (requests/mine/); approved entries
        link to the grant they produced.

   CSP-safe: no inline handlers, DOM built with textContent only, endpoint
   URLs read from the #gate-config JSON block.
   ========================================================================= */
(function () {
  "use strict";

  var cfg = JSON.parse(document.getElementById("gate-config").textContent);
  var docTypes = JSON.parse(document.getElementById("gate-doctypes").textContent);
  var csrfInput = document.querySelector("input[name='csrfmiddlewaretoken']");
  var csrftoken = csrfInput ? csrfInput.value : "";

  var DOC_LABELS = {
    ethics: "Ethics approval",
    proposal: "Project proposal",
    dua: "Data-use agreement"
  };
  var MAX_OPTIONS = 200;   // rendered picker rows; typing narrows the rest

  // -----------------------------------------------------------------------
  // DOM + fetch helpers
  // -----------------------------------------------------------------------
  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") el.textContent = v;
      else if (k === "class") el.className = v;
      else if (k === "data") Object.keys(v).forEach(function (d) { el.dataset[d] = v[d]; });
      else if (k.indexOf("on") === 0 && typeof v === "function") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    });
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return el;
  }
  function byId(id) { return document.getElementById(id); }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  // A non-JSON body (login redirect, HTML error page) becomes a clean error.
  function readJson(res) {
    return res.json().catch(function () { return null; }).then(function (data) {
      if (!res.ok || !data || data.success === false) {
        throw new Error((data && data.error && data.error.message) ||
                        "Request failed (HTTP " + res.status + ")");
      }
      return data;
    });
  }
  function getJson(url) {
    return fetch(url, { credentials: "same-origin",
                        headers: { Accept: "application/json" } }).then(readJson);
  }
  function postForm(url, formData) {
    formData.append("csrfmiddlewaretoken", csrftoken);
    return fetch(url, { method: "POST", body: formData, credentials: "same-origin",
                        headers: { Accept: "application/json",
                                   "X-CSRFToken": csrftoken } }).then(readJson);
  }

  function fmtDate(iso) {
    if (!iso) return "-";
    var d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleDateString(undefined,
      { year: "numeric", month: "short", day: "numeric" });
  }
  function fmtBytes(n) {
    var units = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : String(n)) + " " + units[i];
  }
  function docLabel(t) { return DOC_LABELS[t] || t; }
  function downloadUrl(imageId) { return cfg.download.replace("/0/", "/" + imageId + "/"); }
  function browseUrl(scope, id) { return cfg.index + "access/" + scope + "/" + id + "/"; }
  function docUrl(reqId, name) { return cfg.index + "doc/" + reqId + "/" + encodeURIComponent(name); }
  function chip(kind, text) { return h("span", { class: "chip chip-" + kind, text: text }); }

  var uid = 0;
  function nextId(prefix) { uid += 1; return prefix + "-" + uid; }

  // -----------------------------------------------------------------------
  // 1. New request: dataset picker + id fallback + typed documents
  // -----------------------------------------------------------------------
  var els = {
    form: byId("request-form"),
    type: byId("target-type"),
    datasetField: byId("dataset-field"),
    datasetLabel: byId("dataset-label"),
    picker: byId("dataset-picker"),
    inputWrap: document.querySelector(".picker-input-wrap"),
    search: byId("dataset-search"),
    list: byId("dataset-list"),
    chosen: byId("dataset-chosen"),
    hint: byId("dataset-hint"),
    manualToggle: byId("manual-toggle"),
    idField: byId("id-field"),
    idLabel: byId("target-id-label"),
    idInput: byId("target-id"),
    requiredDocs: byId("required-docs"),
    docs: byId("docs"),
    docRows: byId("doc-rows"),
    submit: byId("submit-btn"),
    message: byId("form-message")
  };

  var picker = {
    all: [],          // datasets from the server, already sorted
    options: [],      // {el, d} for the enabled rows currently rendered
    active: -1,       // index into options
    chosen: null,     // the selected dataset
    manual: false,    // typing an id instead of picking
    admin: false,
    service: true
  };
  var docChoices = [];  // per attached file: chosen document type or ""

  function setHint() {
    var text;
    if (picker.admin) {
      text = "Administrators can already download every dataset, so no request is needed.";
    } else if (!picker.all.length) {
      text = "No datasets are listed for you yet. If you have a dataset ID, enter it instead.";
    } else {
      var open = picker.all.filter(function (d) { return d.access === "requestable"; }).length;
      text = open + " of " + picker.all.length + " listed datasets can be requested.";
      if (!picker.service) {
        text += " Only your own groups are listed (the catalogue needs the portal's service account); any other dataset can still be requested by ID.";
      }
    }
    els.hint.textContent = text;
  }

  function loadDatasets() {
    return getJson(cfg.requestable).then(function (data) {
      picker.all = data.datasets || [];
      picker.admin = !!data.admin;
      picker.service = !!data.service;
      setHint();
      if (document.activeElement === els.search) openList();
    }).catch(function (err) {
      picker.all = [];
      els.hint.textContent = "Could not load the dataset list (" + err.message +
                             "). You can still enter a dataset ID.";
    });
  }

  function matches(d, terms) {
    var hay = (d.name + " " + (d.project_name || "") + " #" + d.id).toLowerCase();
    return terms.every(function (t) { return hay.indexOf(t) !== -1; });
  }

  function renderList() {
    var raw = els.search.value.trim();
    var terms = raw.toLowerCase().split(/\s+/).filter(Boolean);
    var found = picker.all.filter(function (d) { return matches(d, terms); });
    var shown = found.slice(0, MAX_OPTIONS);
    var group;
    clear(els.list);
    picker.options = [];
    shown.forEach(function (d) {
      var g = d.project_name || "";
      if (g !== group) {
        group = g;
        els.list.appendChild(h("li", { class: "picker-group", role: "presentation",
                                       text: g || "Not in a project" }));
      }
      var disabled = d.access !== "requestable";
      var opt = h("li", {
        id: "ds-opt-" + d.id, role: "option", "aria-selected": "false",
        "aria-disabled": disabled ? "true" : null,
        class: "picker-option" + (disabled ? " is-disabled" : "")
      }, [
        h("span", { class: "opt-name", text: d.name }),
        disabled ? chip(d.access === "granted" ? "approved" : "pending",
                        d.access === "granted" ? "access granted" : "request pending") : null,
        h("span", { class: "opt-meta", text: "#" + d.id })
      ]);
      if (!disabled) {
        // mousedown (not click) so the input keeps focus and the list
        // does not close on blur before the choice registers
        opt.addEventListener("mousedown", function (e) { e.preventDefault(); choose(d); });
        picker.options.push({ el: opt, d: d });
      }
      els.list.appendChild(opt);
    });
    if (!found.length) {
      els.list.appendChild(h("li", { class: "picker-empty", role: "presentation",
        text: raw ? "No dataset matches \"" + raw + "\". You can still enter its ID."
                  : "No datasets to show." }));
    } else if (found.length > shown.length) {
      els.list.appendChild(h("li", { class: "picker-more", role: "presentation",
        text: (found.length - shown.length) + " more: keep typing to narrow the list." }));
    }
    picker.active = picker.options.length ? 0 : -1;
    paintActive();
  }

  function paintActive() {
    picker.options.forEach(function (o, i) {
      var on = i === picker.active;
      o.el.classList.toggle("is-active", on);
      o.el.setAttribute("aria-selected", on ? "true" : "false");
    });
    var cur = picker.options[picker.active];
    if (cur) {
      els.search.setAttribute("aria-activedescendant", cur.el.id);
      cur.el.scrollIntoView({ block: "nearest" });
    } else {
      els.search.removeAttribute("aria-activedescendant");
    }
  }

  function openList() {
    if (picker.admin) return;
    renderList();
    els.list.hidden = false;
    els.search.setAttribute("aria-expanded", "true");
  }
  function closeList() {
    els.list.hidden = true;
    els.search.setAttribute("aria-expanded", "false");
    els.search.removeAttribute("aria-activedescendant");
  }

  function choose(d) {
    picker.chosen = d;
    closeList();
    els.search.value = "";
    clear(els.chosen);
    els.chosen.appendChild(h("div", { class: "chosen-body" }, [
      h("span", { class: "chosen-eyebrow", text: d.project_name || "Not in a project" }),
      h("span", { class: "chosen-name", text: d.name }),
      h("span", { class: "chosen-id", text: "Dataset #" + d.id })
    ]));
    els.chosen.appendChild(h("button", { type: "button", class: "btn-secondary",
                                         text: "Change", onclick: function () { unchoose(true); } }));
    els.chosen.hidden = false;
    els.inputWrap.hidden = true;
    renderRequiredDocs();
  }
  function unchoose(focus) {
    picker.chosen = null;
    clear(els.chosen);
    els.chosen.hidden = true;
    els.inputWrap.hidden = false;
    renderRequiredDocs();
    if (focus) els.search.focus();
  }

  function setMode() {
    var isDataset = els.type.value === "dataset";
    var manual = isDataset && picker.manual;
    els.datasetField.hidden = !isDataset;
    els.datasetLabel.hidden = manual;
    els.picker.hidden = manual;
    els.hint.hidden = manual;
    els.manualToggle.textContent = manual ? "Choose from the list instead"
                                          : "Enter a dataset ID instead";
    els.idField.hidden = isDataset && !manual;
    els.idLabel.textContent = isDataset ? "Dataset ID" : "Image ID";
    renderRequiredDocs();
  }

  // Required types come from the chosen dataset's policy; with a typed-in
  // id the server reports any that are missing instead.
  function requiredDocs() {
    if (els.type.value !== "dataset" || picker.manual || !picker.chosen) return [];
    return picker.chosen.required_docs || [];
  }

  function renderRequiredDocs() {
    var req = requiredDocs();
    clear(els.requiredDocs);
    els.requiredDocs.hidden = !req.length;
    if (req.length) {
      els.requiredDocs.appendChild(document.createTextNode("This dataset's policy requires: "));
      req.forEach(function (t, i) {
        if (i) els.requiredDocs.appendChild(document.createTextNode(", "));
        els.requiredDocs.appendChild(h("strong", { text: docLabel(t) }));
      });
      els.requiredDocs.appendChild(document.createTextNode(". Tag each file with its type below."));
    }
    renderDocRows();
  }

  function renderDocRows() {
    var files = Array.prototype.slice.call(els.docs.files || []);
    var req = requiredDocs();
    clear(els.docRows);
    els.docRows.hidden = !files.length;
    files.forEach(function (f, i) {
      var options = [h("option", { value: "", text: "Document type..." })];
      docTypes.forEach(function (t) {
        options.push(h("option", { value: t, text: docLabel(t) + (req.indexOf(t) !== -1 ? " (required)" : "") }));
      });
      var sel = h("select", { "aria-label": "Document type for " + f.name }, options);
      sel.value = docChoices[i] || "";
      sel.addEventListener("change", function () { docChoices[i] = sel.value; });
      els.docRows.appendChild(h("li", { class: "doc-row" }, [
        h("span", { class: "doc-name", text: f.name }),
        h("span", { class: "doc-size", text: fmtBytes(f.size) }),
        sel
      ]));
    });
  }

  function showMessage(text, kind) {
    els.message.textContent = text;
    els.message.className = "message " + kind;
    els.message.hidden = false;
  }

  function submitRequest(e) {
    e.preventDefault();
    els.message.hidden = true;
    var type = els.type.value;
    var targetId;
    if (type === "dataset" && !picker.manual) {
      if (!picker.chosen) {
        showMessage("Choose a dataset from the list, or enter its ID instead.", "err");
        els.search.focus();
        return;
      }
      targetId = picker.chosen.id;
    } else {
      targetId = parseInt(els.idInput.value, 10);
      if (!(targetId > 0)) {
        showMessage("Enter the numeric " + type + " ID.", "err");
        els.idInput.focus();
        return;
      }
    }
    var files = Array.prototype.slice.call(els.docs.files || []);
    var tagged = docChoices.filter(Boolean);
    var missing = requiredDocs().filter(function (t) { return tagged.indexOf(t) === -1; });
    if (missing.length) {
      showMessage("This dataset requires: " + missing.map(docLabel).join(", ") +
                  ". Attach the document(s) and tag their type.", "err");
      return;
    }

    var fd = new FormData();
    fd.append("target_type", type);
    fd.append("target_id", String(targetId));
    fd.append("reason", byId("reason").value);
    files.forEach(function (f, i) {
      fd.append("doc" + i, f);
      if (docChoices[i]) fd.append("doctype" + i, docChoices[i]);
    });

    els.submit.disabled = true;
    postForm(cfg.request, fd).then(function () {
      els.form.reset();
      docChoices = [];
      picker.manual = false;
      unchoose(false);
      setMode();
      showMessage("Request submitted. A data steward will review it; track it under My requests.", "ok");
      loadDatasets();
      loadRequests();
    }).catch(function (err) {
      showMessage(err.message, "err");
    }).then(function () {
      els.submit.disabled = false;
    });
  }

  els.type.addEventListener("change", setMode);
  els.manualToggle.addEventListener("click", function () {
    picker.manual = !picker.manual;
    setMode();
    (picker.manual ? els.idInput : els.search).focus();
  });
  els.search.addEventListener("focus", openList);
  els.search.addEventListener("input", openList);
  els.search.addEventListener("blur", closeList);
  els.search.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (els.list.hidden) { openList(); return; }
      if (!picker.options.length) return;
      var step = e.key === "ArrowDown" ? 1 : -1;
      picker.active = (picker.active + step + picker.options.length) % picker.options.length;
      paintActive();
    } else if (e.key === "Enter") {
      // choose instead of submitting the form while the list is open
      if (!els.list.hidden && picker.options[picker.active]) {
        e.preventDefault();
        choose(picker.options[picker.active].d);
      }
    } else if (e.key === "Escape" && !els.list.hidden) {
      e.preventDefault();
      closeList();
    }
  });
  els.docs.addEventListener("change", function () { docChoices = []; renderDocRows(); });
  els.form.addEventListener("submit", submitRequest);

  // -----------------------------------------------------------------------
  // 2. My access: grants -> projects / datasets -> images
  // -----------------------------------------------------------------------
  var grantsByRequest = {};   // request id -> grant id, for "My requests" links

  function expiryText(iso) { return iso ? "until " + fmtDate(iso) : "standing"; }

  function toggleRow(label, scope, id, meta) {
    var panelId = nextId("panel");
    var toggle = h("button", { type: "button", class: "tree-toggle",
                               "aria-expanded": "false", "aria-controls": panelId }, [
      h("span", { class: "chevron", "aria-hidden": "true" }),
      chip("scope", scope),
      h("span", { class: "access-name", text: label })
    ]);
    var panel = h("div", { class: "tree-panel", id: panelId, hidden: true });
    toggle.addEventListener("click", function () { toggleNode(toggle, panel, scope, id); });
    var head = h("div", { class: "access-head" }, [toggle,
      h("span", { class: "access-meta", text: meta })]);
    return { head: head, panel: panel, toggle: toggle };
  }

  function grantRow(g) {
    var label = g.name || (g.scope_type + " #" + g.scope_id);
    var meta = "#" + g.scope_id + ", " + expiryText(g.expires_at);
    var li = h("li", { class: "access-item", id: "grant-" + g.id });
    if (!g.reachable) {
      li.appendChild(h("div", { class: "access-head" }, [
        chip("scope", g.scope_type),
        h("span", { class: "access-name", text: label }),
        h("span", { class: "access-meta", text: "not reachable, ask an administrator" })
      ]));
      return li;
    }
    if (g.scope_type === "image") {
      li.appendChild(h("div", { class: "access-head" }, [
        chip("scope", "image"),
        h("span", { class: "access-name", text: label }),
        h("span", { class: "access-meta", text: meta }),
        h("a", { class: "btn-download", href: downloadUrl(g.scope_id), text: "Download" })
      ]));
      return li;
    }
    var row = toggleRow(label, g.scope_type, g.scope_id, meta);
    li.appendChild(row.head);
    li.appendChild(row.panel);
    return li;
  }

  function toggleNode(toggle, panel, scope, id) {
    var open = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", open ? "false" : "true");
    panel.hidden = open;
    if (open || panel.dataset.loaded) return;
    panel.dataset.loaded = "1";
    clear(panel);
    panel.appendChild(h("p", { class: "muted loading", text: "Loading..." }));
    getJson(browseUrl(scope, id)).then(function (data) {
      clear(panel);
      renderChildren(panel, data);
    }).catch(function (err) {
      clear(panel);
      delete panel.dataset.loaded;   // collapse + expand retries
      panel.appendChild(h("p", { class: "muted", text: err.message }));
    });
  }

  function renderChildren(panel, data) {
    var items = data.items || [];
    if (!items.length) {
      panel.appendChild(h("p", { class: "muted",
        text: data.scope === "project" ? "This project has no datasets."
                                       : "No images in this dataset." }));
      return;
    }
    var ul = h("ul", { class: "tree-children" });
    items.forEach(function (item) {
      if (item.kind === "dataset") {
        var row = toggleRow(item.name, "dataset", item.id, "#" + item.id);
        ul.appendChild(h("li", { class: "access-item" }, [row.head, row.panel]));
        return;
      }
      ul.appendChild(h("li", { class: "image-row" }, [
        h("span", { class: "image-name", text: item.name }),
        h("span", { class: "access-meta", text: "#" + item.id }),
        item.can_download
          ? h("a", { class: "btn-download", href: downloadUrl(item.id), text: "Download" })
          : h("span", { class: "muted", text: "not covered" })
      ]));
    });
    panel.appendChild(ul);
  }

  function openFromHash() {
    var m = /^#grant-([0-9a-f]{32})$/.exec(window.location.hash);
    if (!m) return;
    var li = byId("grant-" + m[1]);
    if (!li) return;
    Array.prototype.forEach.call(document.querySelectorAll(".access-item.is-target"),
      function (el) { el.classList.remove("is-target"); });
    li.classList.add("is-target");
    var toggle = li.querySelector(".tree-toggle");
    if (toggle && toggle.getAttribute("aria-expanded") !== "true") toggle.click();
    li.scrollIntoView({ block: "center" });
  }

  function loadAccess() {
    var box = byId("access-list");
    return getJson(cfg.myAccess).then(function (data) {
      var grants = data.access || [];
      grantsByRequest = {};
      grants.forEach(function (g) { if (g.request_id) grantsByRequest[g.request_id] = g.id; });
      clear(box);
      if (!grants.length) {
        box.appendChild(h("p", { class: "muted",
          text: "No active access yet. Approved requests appear here." }));
        return;
      }
      var ul = h("ul", { class: "access-tree" });
      grants.forEach(function (g) { ul.appendChild(grantRow(g)); });
      box.appendChild(ul);
      openFromHash();
    }).catch(function (err) {
      clear(box);
      box.appendChild(h("p", { class: "muted",
        text: "Could not load your access (" + err.message + ")." }));
    });
  }

  // -----------------------------------------------------------------------
  // 3. My requests
  // -----------------------------------------------------------------------
  function requestRow(r) {
    var row = h("div", { class: "request-row" }, [
      h("div", { class: "request-head" }, [
        chip(r.status, r.status),
        h("strong", { text: r.target_type + " #" + r.target_id }),
        h("span", { class: "muted", text: "requested " + fmtDate(r.created_at) })
      ])
    ]);
    if (r.review_note) {
      row.appendChild(h("div", { class: "muted", text: "Reviewer note: " + r.review_note }));
    }
    if (r.status === "approved") {
      var gid = grantsByRequest[r.id];
      if (gid) {
        row.appendChild(h("div", { class: "request-access" }, [
          h("a", { href: "#grant-" + gid, text: "Open in My access",
                   // same-hash clicks fire no hashchange; open explicitly
                   onclick: function () { setTimeout(openFromHash, 0); } }),
          h("span", { class: "muted", text: " (access " + expiryText(r.expires_at) + ")" })
        ]));
      } else {
        row.appendChild(h("div", { class: "muted",
          text: "The access from this approval has expired or been revoked." }));
      }
    }
    if (r.documents && r.documents.length) {
      var docs = h("div", { class: "muted request-docs" }, ["Documents: "]);
      r.documents.forEach(function (name, i) {
        if (i) docs.appendChild(document.createTextNode(", "));
        docs.appendChild(h("a", { href: docUrl(r.id, name), text: name }));
      });
      row.appendChild(docs);
    }
    return row;
  }

  function loadRequests() {
    var box = byId("requests-list");
    return getJson(cfg.mine).then(function (data) {
      var reqs = data.requests || [];
      clear(box);
      if (!reqs.length) {
        box.appendChild(h("p", { class: "muted", text: "No requests yet." }));
        return;
      }
      reqs.forEach(function (r) { box.appendChild(requestRow(r)); });
    }).catch(function (err) {
      clear(box);
      box.appendChild(h("p", { class: "muted",
        text: "Could not load requests (" + err.message + ")." }));
    });
  }

  // -----------------------------------------------------------------------
  // Boot: access first, so request rows can link to their grants
  // -----------------------------------------------------------------------
  window.addEventListener("hashchange", openFromHash);
  setMode();
  loadDatasets();
  loadAccess().then(loadRequests);
})();
