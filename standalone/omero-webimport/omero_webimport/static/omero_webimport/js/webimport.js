/* =========================================================================
   OMERO Web Import - uploads with per-file progress (index.html)

   Flow:
     1. upload/begin/      -> an upload id
     2. upload/chunk/      -> each file in 64 MiB chunks over XHR (byte-level
                              progress), every chunk retried on its own;
                              each file declares its total_size
     3. upload/complete/   -> an import job (or, in sync mode, the results)
     4. upload/status/<id> -> polled; per-file states map back onto rows

   Every file is a row with its own bar and phase:
     ready -> uploading -> uploaded -> waiting -> transferring (bytes into
     OMERO) -> processing (server-side import) -> imported | failed
   One failure never stops the rest. "Retry failed files" re-imports only
   the failures, from the copy already staged on the server.

   CSP-safe: no inline handlers, DOM built with textContent, endpoint URLs
   read from the #webimport-config JSON block.
   ========================================================================= */
(function () {
  "use strict";

  var cfg = JSON.parse(document.getElementById("webimport-config").textContent);
  var csrfInput = document.querySelector("input[name='csrfmiddlewaretoken']");
  var csrftoken = csrfInput ? csrfInput.value : "";

  var CHUNK_SIZE = 64 * 1024 * 1024;  // a dropped connection retries one chunk, not the file
  var CHUNK_RETRIES = 3;
  var POLL_MS = 1500;
  var POLL_GIVE_UP = 40;              // consecutive failed polls (~1 min) before giving up

  var STATE_LABELS = {
    ready: "Ready",
    uploading: "Uploading",
    uploaded: "Uploaded",
    "upload-failed": "Upload failed",
    waiting: "Waiting to import",
    transferring: "Into OMERO",
    processing: "Processing",
    done: "Imported",
    failed: "Import failed"
  };
  var SETTLED = ["done", "failed", "upload-failed"];

  // -----------------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------------
  function h(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") node.textContent = v;
      else if (k === "class") node.className = v;
      else if (k.indexOf("on") === 0 && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    });
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }
  function byId(id) { return document.getElementById(id); }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function fmtBytes(n) {
    var units = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(n < 10 ? 1 : 0) : String(n)) + " " + units[i];
  }

  // A non-JSON body (login redirect, HTML error page) becomes a clean error.
  function readJson(res) {
    return res.json().catch(function () { return null; }).then(function (data) {
      if (!res.ok || !data || data.success === false) {
        throw new Error((data && data.error && data.error.message) ||
                        "Server error (HTTP " + res.status + ")");
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

  // -----------------------------------------------------------------------
  // State
  // -----------------------------------------------------------------------
  var el = {
    zone: byId("drop-zone"),
    input: byId("file-input"),
    batch: byId("batch"),
    summary: byId("batch-summary"),
    meter: byId("batch-meter"),
    list: byId("file-list"),
    dataset: byId("dataset-target"),
    importBtn: byId("import-btn"),
    retryBtn: byId("retry-btn"),
    notice: byId("notice")
  };
  var entries = [];        // one per file row; entries[i].index === file_index i
  var phase = "idle";      // idle | running | finished
  var uploadId = null;
  var lastWarning = null;  // server warning from the latest import (e.g. linking)
  var rate = { ema: 0, t: 0, bytes: 0 };   // smoothed upload speed, bytes/s

  // -----------------------------------------------------------------------
  // Rendering
  // -----------------------------------------------------------------------
  function makeEntry(file) {
    var entry = { file: file, index: -1, state: "ready", sent: 0, importSent: 0,
                  imageIds: [], error: null };
    var fill = h("span", { class: "bar-fill" });
    entry.parts = {
      state: h("span", { class: "file-state" }),
      remove: h("button", { type: "button", class: "file-remove", text: "×",
                            "aria-label": "Remove " + file.name,
                            onclick: function () { removeEntry(entry); } }),
      fill: fill,
      bar: h("div", { class: "bar", role: "progressbar", "aria-label": file.name,
                      "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": "0" },
             [fill]),
      detail: h("div", { class: "file-detail" })
    };
    entry.el = h("li", { class: "file-row" }, [
      h("div", { class: "file-top" }, [
        h("span", { class: "file-name", text: file.name, title: file.name }),
        h("span", { class: "file-size", text: fmtBytes(file.size) }),
        entry.parts.state,
        entry.parts.remove
      ]),
      entry.parts.bar,
      entry.parts.detail
    ]);
    return entry;
  }

  function paintEntry(e) {
    var p = e.parts, size = e.file.size, pct = 0, detail = "";
    e.el.setAttribute("data-state", e.state);
    p.state.textContent = STATE_LABELS[e.state] || e.state;
    p.remove.hidden = phase !== "idle";
    switch (e.state) {
      case "uploading":
        pct = size ? e.sent / size * 100 : 0;
        detail = fmtBytes(e.sent) + " of " + fmtBytes(size) + " uploaded";
        break;
      case "uploaded":
        pct = 100; detail = "Staged on the server"; break;
      case "waiting":
        pct = 100; detail = "Queued behind earlier files"; break;
      case "transferring":
        pct = size ? e.importSent / size * 100 : 0;
        detail = fmtBytes(e.importSent) + " of " + fmtBytes(size) + " into the OMERO repository";
        break;
      case "processing":
        pct = 100; detail = "Reading the slide and building thumbnails…"; break;
      case "done":
      case "failed":
      case "upload-failed":
        pct = 100; detail = e.error || ""; break;
    }
    p.fill.style.width = Math.min(100, pct).toFixed(1) + "%";
    p.bar.setAttribute("aria-valuenow", String(Math.round(pct)));
    clear(p.detail);
    if (e.state === "done" && e.imageIds.length) {
      var many = e.imageIds.length > 1;
      p.detail.appendChild(h("a", {
        href: cfg.webclient + "?show=image-" + e.imageIds[0],
        text: many ? "View " + e.imageIds.length + " images →"
                   : "View image #" + e.imageIds[0] + " →"
      }));
    } else {
      p.detail.textContent = detail;
    }
  }

  function count(state) {
    return entries.filter(function (e) { return e.state === state; }).length;
  }
  function settled(e) { return SETTLED.indexOf(e.state) !== -1; }
  function uploadedBytes(e) {
    if (e.state === "ready") return 0;
    if (e.state === "uploading") return e.sent;
    return e.file.size;   // staged (or settled) - its upload share is complete
  }

  function paintBatch() {
    el.batch.hidden = !entries.length;
    var total = entries.reduce(function (a, e) { return a + e.file.size; }, 0);
    var up = entries.reduce(function (a, e) { return a + uploadedBytes(e); }, 0);
    var finished = entries.filter(settled).length;
    // one monotonic meter: first half uploads (bytes), second half imports
    var frac = phase === "idle" ? 0 :
      (total ? up / total : 0) * 0.5 + (entries.length ? finished / entries.length : 0) * 0.5;
    el.meter.style.width = (frac * 100).toFixed(1) + "%";

    var text;
    if (phase === "idle") {
      text = entries.length + (entries.length === 1 ? " file" : " files") +
             " · " + fmtBytes(total);
    } else if (phase === "running" && entries.some(function (e) { return e.state === "uploading"; })) {
      var cur = entries.filter(function (e) { return e.state === "uploading"; })[0];
      text = "Uploading " + (entries.indexOf(cur) + 1) + " of " + entries.length +
             " · " + fmtBytes(up) + " of " + fmtBytes(total) +
             (rate.ema ? " · " + fmtBytes(rate.ema) + "/s" : "");
    } else if (phase === "running") {
      text = "Importing into OMERO · " + finished + " of " + entries.length + " finished";
    } else {
      text = count("done") + " imported" +
             (count("failed") ? " · " + count("failed") + " failed" : "") +
             (count("upload-failed") ? " · " + count("upload-failed") + " not uploaded" : "");
    }
    el.summary.textContent = text;
  }

  function paintAll() { entries.forEach(paintEntry); paintBatch(); }

  function showNotice(text, kind, link) {
    clear(el.notice);
    el.notice.className = "notice notice-" + kind;
    el.notice.appendChild(document.createTextNode(text));
    if (link) {
      el.notice.appendChild(document.createTextNode(" "));
      el.notice.appendChild(link);
    }
    el.notice.hidden = false;
  }
  function hideNotice() { el.notice.hidden = true; clear(el.notice); }

  // -----------------------------------------------------------------------
  // Selecting files
  // -----------------------------------------------------------------------
  function addFiles(list) {
    if (phase === "running") return;
    if (phase === "finished") {   // a new selection starts a fresh batch
      entries = [];
      clear(el.list);
      uploadId = null;
      phase = "idle";
      el.retryBtn.hidden = true;
      hideNotice();
    }
    var empty = [];
    Array.prototype.forEach.call(list || [], function (f) {
      if (!f.size) { empty.push(f.name); return; }
      var e = makeEntry(f);
      entries.push(e);
      el.list.appendChild(e.el);
      paintEntry(e);
    });
    if (empty.length) showNotice("Skipped empty file(s): " + empty.join(", ") + ".", "warn");
    el.importBtn.disabled = !entries.length;
    paintBatch();
  }

  function removeEntry(e) {
    if (phase !== "idle") return;
    entries.splice(entries.indexOf(e), 1);
    el.list.removeChild(e.el);
    el.importBtn.disabled = !entries.length;
    paintBatch();
  }

  // -----------------------------------------------------------------------
  // Uploading (XHR: fetch has no upload progress events)
  // -----------------------------------------------------------------------
  function sampleRate() {
    var now = performance.now();
    var bytes = entries.reduce(function (a, e) { return a + uploadedBytes(e); }, 0);
    if (!rate.t) { rate.t = now; rate.bytes = bytes; return; }
    var dt = (now - rate.t) / 1000;
    if (dt < 0.5) return;
    var inst = Math.max(0, (bytes - rate.bytes) / dt);
    rate.ema = rate.ema ? rate.ema * 0.7 + inst * 0.3 : inst;
    rate.t = now;
    rate.bytes = bytes;
  }

  function sendChunk(entry, chunkIndex, blob, base) {
    return new Promise(function (resolve, reject) {
      var fd = new FormData();
      fd.append("csrfmiddlewaretoken", csrftoken);
      fd.append("upload_id", uploadId);
      fd.append("file_index", String(entry.index));
      fd.append("chunk_index", String(chunkIndex));
      fd.append("filename", entry.file.name);
      fd.append("total_size", String(entry.file.size));
      fd.append("chunk", blob, entry.file.name + ".part");
      var xhr = new XMLHttpRequest();
      xhr.open("POST", cfg.chunk);
      xhr.setRequestHeader("X-CSRFToken", csrftoken);
      xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.addEventListener("progress", function (ev) {
        if (!ev.lengthComputable || !ev.total) return;
        // ev counts the whole multipart body; scale it to the chunk's bytes
        entry.sent = base + Math.min(blob.size, Math.round(ev.loaded / ev.total * blob.size));
        sampleRate();
        paintEntry(entry);
        paintBatch();
      });
      xhr.addEventListener("load", function () {
        var data = null;
        try { data = JSON.parse(xhr.responseText); } catch (err) { /* HTML error page */ }
        if (xhr.status >= 200 && xhr.status < 300 && data && data.success !== false) {
          resolve(data);
        } else {
          reject(new Error((data && data.error && data.error.message) ||
                           "Server error (HTTP " + xhr.status + ")"));
        }
      });
      xhr.addEventListener("error", function () { reject(new Error("Network error")); });
      xhr.addEventListener("abort", function () { reject(new Error("Upload aborted")); });
      xhr.send(fd);
    });
  }

  async function uploadFile(entry) {
    var size = entry.file.size;
    var chunks = Math.max(1, Math.ceil(size / CHUNK_SIZE));
    for (let ci = 0; ci < chunks; ci++) {
      let start = ci * CHUNK_SIZE;
      let blob = entry.file.slice(start, start + CHUNK_SIZE);
      for (let attempt = 1; ; attempt++) {
        try {
          // a re-sent chunk the server already holds is acknowledged as a
          // duplicate, so retrying after a lost response is safe
          await sendChunk(entry, ci, blob, start);
          break;
        } catch (err) {
          if (attempt >= CHUNK_RETRIES) throw err;
          await sleep(1000 * attempt);
        }
      }
      entry.sent = Math.min(size, start + blob.size);
    }
  }

  // -----------------------------------------------------------------------
  // Importing: complete/ + status polling
  // -----------------------------------------------------------------------
  function applyServerFile(e, f) {
    // the job's "queued" means staged and waiting its turn to import
    e.state = f.state === "queued" ? "waiting" : f.state;
    e.importSent = f.sent || 0;
    e.imageIds = f.image_ids || [];
    e.error = f.error || null;
    paintEntry(e);
  }

  function poll(statusUrl, mapping) {
    return new Promise(function (resolve) {
      var failures = 0;
      function tick() {
        getJson(statusUrl).then(function (job) {
          failures = 0;
          (job.files || []).forEach(function (f, k) {
            if (mapping[k]) applyServerFile(mapping[k], f);
          });
          paintBatch();
          if (job.status === "running") setTimeout(tick, POLL_MS);
          else resolve(job);
        }).catch(function (err) {
          failures += 1;
          if (failures >= POLL_GIVE_UP) {
            resolve({ status: "lost", error: "Lost contact with the server (" + err.message +
                      "). The import may still finish — check the webclient." });
          } else {
            setTimeout(tick, POLL_MS);
          }
        });
      }
      setTimeout(tick, 600);
    });
  }

  function failUnsettled(message) {
    entries.forEach(function (e) {
      if (!settled(e) && e.state !== "ready") {
        e.state = "failed";
        e.error = message;
        paintEntry(e);
      }
    });
  }

  async function importStaged() {
    var fd = new FormData();
    fd.append("upload_id", uploadId);
    if (el.dataset.value) fd.append("dataset_id", el.dataset.value);
    var res = await postForm(cfg.complete, fd);
    var mapping = (res.file_indexes || []).map(function (i) { return entries[i]; });
    (res.skipped || []).forEach(function (i) {
      var e = entries[i];
      if (e && e.state !== "upload-failed") {
        e.state = "upload-failed";
        e.error = "The upload did not finish — add the file again.";
        paintEntry(e);
      }
    });
    lastWarning = null;
    if (res.job_id) {
      mapping.forEach(function (e) { if (e) { e.state = "waiting"; paintEntry(e); } });
      paintBatch();
      var job = await poll(res.status_url, mapping);
      lastWarning = job.warning || null;
      if (job.status === "lost") {
        failUnsettled("Status unknown");
        throw new Error(job.error);
      }
      if (job.status === "failed" && !(job.files && job.files.length)) {
        failUnsettled(job.error || "Import failed");
      }
    } else {
      // synchronous mode (WEBIMPORT_ASYNC=0): results come with the response
      (res.files || []).forEach(function (r, k) {
        var e = mapping[k];
        if (!e) return;
        e.state = r.error ? "failed" : "done";
        e.imageIds = r.image_ids || [];
        e.error = r.error || null;
        paintEntry(e);
      });
      lastWarning = res.warning || null;
    }
  }

  function finish() {
    phase = "finished";
    var done = count("done"), failed = count("failed"), notUp = count("upload-failed");
    el.retryBtn.hidden = !(failed && uploadId);
    el.importBtn.disabled = true;   // a new selection starts the next batch
    paintAll();
    var parts = [];
    if (done) parts.push(done + (done === 1 ? " slide" : " slides") + " imported");
    if (failed) parts.push(failed + " failed to import");
    if (notUp) parts.push(notUp + " did not upload");
    if (!parts.length) return;
    var text = parts.join(" · ") + "." +
      (failed ? " Retry the failed files, or ask an administrator to check the server logs." : "") +
      (lastWarning ? " " + lastWarning : "");
    var kind = failed || notUp ? (done ? "warn" : "error") : "ok";
    showNotice(text, kind,
               done ? h("a", { href: cfg.webclient, text: "Open the webclient →" }) : null);
  }

  async function run() {
    phase = "running";
    el.importBtn.disabled = true;
    el.retryBtn.hidden = true;
    hideNotice();
    rate = { ema: 0, t: 0, bytes: 0 };
    paintAll();
    try {
      var begin = await postForm(cfg.begin, new FormData());
      uploadId = begin.upload_id;
      entries.forEach(function (e, i) { e.index = i; });
      for (var i = 0; i < entries.length; i++) {
        var e = entries[i];
        e.state = "uploading";
        e.sent = 0;
        paintEntry(e);
        paintBatch();
        try {
          await uploadFile(e);
          e.state = "uploaded";
        } catch (err) {
          e.state = "upload-failed";
          e.error = "Upload failed: " + err.message;
        }
        paintEntry(e);
        paintBatch();
      }
      if (entries.some(function (x) { return x.state === "uploaded"; })) {
        await importStaged();
      }
      finish();
    } catch (err) {
      failUnsettled(err.message);
      finish();
      showNotice(err.message, "error");
    }
  }

  async function retryFailed() {
    phase = "running";
    el.retryBtn.hidden = true;
    hideNotice();
    entries.forEach(function (e) {
      if (e.state === "failed") {
        e.state = "waiting";
        e.error = null;
        e.importSent = 0;
      }
    });
    paintAll();
    try {
      await importStaged();
      finish();
    } catch (err) {
      failUnsettled(err.message);
      finish();
      showNotice(err.message, "error");
    }
  }

  // -----------------------------------------------------------------------
  // Wiring
  // -----------------------------------------------------------------------
  var dragDepth = 0;   // dragenter/leave also fire for child elements
  el.zone.addEventListener("click", function () { if (phase !== "running") el.input.click(); });
  el.zone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (phase !== "running") el.input.click();
    }
  });
  el.zone.addEventListener("dragenter", function (e) {
    e.preventDefault();
    dragDepth += 1;
    el.zone.classList.add("is-over");
  });
  el.zone.addEventListener("dragover", function (e) { e.preventDefault(); });
  el.zone.addEventListener("dragleave", function () {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) el.zone.classList.remove("is-over");
  });
  el.zone.addEventListener("drop", function (e) {
    e.preventDefault();
    dragDepth = 0;
    el.zone.classList.remove("is-over");
    addFiles(e.dataTransfer.files);
  });
  el.input.addEventListener("change", function () {
    addFiles(el.input.files);
    el.input.value = "";   // allow re-selecting the same file
  });
  el.importBtn.addEventListener("click", run);
  el.retryBtn.addEventListener("click", retryFailed);
  window.addEventListener("beforeunload", function (e) {
    // only the upload needs this page; a running import continues server-side
    if (entries.some(function (x) { return x.state === "uploading"; })) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  getJson(cfg.datasets).then(function (data) {
    (data.datasets || []).forEach(function (d) {
      el.dataset.appendChild(h("option", { value: String(d.id), text: d.name + " (#" + d.id + ")" }));
    });
  }).catch(function () { /* keep "Unassigned"; import still works */ });

  paintBatch();
})();
