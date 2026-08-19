// No framework: the server is the source of truth for every job's HTML.
// This file's whole job is wiring interactions and swapping small pieces of
// the DOM in response to them, via three primitives -
//
//   - loadPanel(id)     fetch + swap the one detail panel currently shown
//   - refreshRail()     fetch + swap the whole queue rail
//   - refreshBoard(id)  do both, then re-select `id` (or the next best job
//                        if `id` no longer exists, e.g. after a remove)
//
// Only one job's panel ever lives in the DOM at a time (see loadPanel) -
// panels for jobs you're not looking at are never pre-rendered, so nothing
// can go stale behind your back after a reorder or a background status
// change. pollStatus() below is what notices those background changes: it
// hits a cheap JSON endpoint every 2.5s, patches progress numbers in place
// for the common case (nothing but percent/stage changed), and falls back
// to a full refreshBoard() only when a job's status actually moved it
// between groups or a job appeared/disappeared.
(function () {
  "use strict";

  var detail = document.getElementById("detail");
  var currentPanelId = null;
  var knownStatuses = {}; // job id -> status, used to detect structural changes while polling

  // Mirrors _NEEDS_INPUT_STATUSES / _CONVERTING_STATUSES in app/web/routes.py.
  var NEEDS_INPUT_STATUSES = [
    "pending",
    "queued",
    "detecting",
    "awaiting_metadata_confirm",
    "failed",
    "cancelled",
  ];
  var CONVERTING_STATUSES = ["ready", "processing"];

  function updateLivePill(jobs) {
    var pillText = document.getElementById("livePillText");
    if (!pillText) return;
    var convertingCount = 0;
    var needsInputCount = 0;
    jobs.forEach(function (j) {
      if (CONVERTING_STATUSES.indexOf(j.status) !== -1) convertingCount++;
      else if (NEEDS_INPUT_STATUSES.indexOf(j.status) !== -1) needsInputCount++;
    });
    pillText.textContent = convertingCount + " converting/queued · " + needsInputCount + " need input";
  }

  function post(url, formData) {
    return fetch(url, { method: "POST", body: formData || new FormData() }).then(function (res) {
      if (!res.ok) throw new Error("Request failed: " + url);
      return res;
    });
  }

  function getHtml(url) {
    return fetch(url).then(function (res) {
      if (!res.ok) throw new Error("Request failed: " + url);
      return res.text();
    });
  }

  function getJson(url) {
    return fetch(url).then(function (res) {
      if (!res.ok) throw new Error("Request failed: " + url);
      return res.json();
    });
  }

  // ---- waveform bars (decorative, built fresh whenever a panel/bar mounts) ----
  function buildBars(container, count) {
    if (!container || container.childElementCount) return;
    var frag = document.createDocumentFragment();
    for (var i = 0; i < count; i++) {
      var b = document.createElement("span");
      b.className = "bar";
      var h = 30 + Math.round(Math.random() * 65);
      b.style.setProperty("--h", h + "%");
      b.style.animationDelay = (Math.random() * 1.1).toFixed(2) + "s";
      frag.appendChild(b);
    }
    container.appendChild(frag);
  }

  function buildAllWaveforms(root) {
    root.querySelectorAll("[data-wave-bg]").forEach(function (el) {
      buildBars(el, 56);
    });
    root.querySelectorAll("[data-wave-fg]").forEach(function (el) {
      buildBars(el, 56);
    });
    var ncWave = root.querySelector("#ncWave");
    if (ncWave) buildBars(ncWave, 22);
  }

  // ---- panel loading (on demand - only one panel ever lives in the DOM) ----
  function loadPanel(jobId) {
    return getHtml("/fragments/panel/" + jobId).then(function (html) {
      detail.innerHTML = html;
      currentPanelId = String(jobId);
      buildAllWaveforms(detail);
      wirePanelInteractions(detail);
    });
  }

  function selectItem(jobId) {
    document.querySelectorAll(".queue-item").forEach(function (i) {
      i.classList.toggle("active", i.getAttribute("data-target") === String(jobId));
    });
    if (String(jobId) !== currentPanelId) {
      loadPanel(jobId);
    }
  }

  // ---- rail + now-converting refresh ----
  function refreshRail() {
    return getHtml("/fragments/rail").then(function (html) {
      var rail = document.getElementById("rail");
      rail.outerHTML = html;
      wireRailInteractions();
      var activeItem = document.querySelector(".queue-item.active");
      if (activeItem) activeItem.classList.add("active");
    });
  }

  function refreshNowConverting() {
    return getHtml("/fragments/now-converting").then(function (html) {
      var wrapper = document.createElement("div");
      wrapper.innerHTML = html;
      var next = wrapper.firstElementChild;
      var current = document.getElementById("nowConverting");
      if (current && next) {
        current.replaceWith(next);
        buildAllWaveforms(document.querySelector(".topbar"));
        wireNowConverting();
      }
    });
  }

  function refreshBoard(focusJobId) {
    return Promise.all([refreshRail(), refreshNowConverting()]).then(function () {
      if (!focusJobId) return;
      var stillExists = document.querySelector('.queue-item[data-target="' + focusJobId + '"]');
      if (stillExists) {
        stillExists.classList.add("active");
        return loadPanel(focusJobId);
      }
      var next = document.querySelector(".queue-item");
      if (next) {
        next.classList.add("active");
        return loadPanel(next.getAttribute("data-target"));
      }
      detail.innerHTML =
        '<div class="card"><p class="waiting-note">Nothing here yet. Drop an audiobook into the inbox folder to get started.</p></div>';
      currentPanelId = null;
    });
  }

  // ---- rail interactions (selection, collapse, reorder) ----
  function wireRailInteractions() {
    document.querySelectorAll(".queue-item").forEach(function (item) {
      item.setAttribute("tabindex", "0");
      item.addEventListener("click", function (e) {
        if (e.target.closest(".reorder")) return;
        selectItem(item.getAttribute("data-target"));
      });
      item.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          item.click();
        }
      });
    });

    document.querySelectorAll(".rail-header[data-collapse-target]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var expanded = btn.getAttribute("aria-expanded") === "true";
        btn.setAttribute("aria-expanded", expanded ? "false" : "true");
        var list = document.getElementById(btn.getAttribute("data-collapse-target"));
        if (list) list.classList.toggle("collapsed", expanded);
      });
    });

    document.querySelectorAll(".reorder-up, .reorder-down").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        if (btn.disabled) return;
        var jobId = btn.getAttribute("data-job-id");
        var direction = btn.getAttribute("data-direction");
        var fd = new FormData();
        fd.append("direction", direction);
        post("/jobs/" + jobId + "/reorder", fd).then(function () {
          refreshBoard(currentPanelId);
        });
      });
    });
  }

  function wireNowConverting() {
    var el = document.getElementById("nowConverting");
    if (el && !el.hidden) {
      el.addEventListener("click", function () {
        var target = el.getAttribute("data-jump-target");
        if (target) selectItem(target);
      });
    }
  }

  // ---- panel interactions (candidates, confirm form, action buttons) ----
  function wirePanelInteractions(root) {
    var candidatesEl = root.querySelector(".candidates[data-candidates-json]");
    if (candidatesEl) {
      var candidates = [];
      try {
        candidates = JSON.parse(candidatesEl.getAttribute("data-candidates-json"));
      } catch (e) {
        candidates = [];
      }
      root.querySelectorAll(".candidate").forEach(function (card) {
        card.setAttribute("tabindex", "0");
        card.setAttribute("role", "radio");
        card.addEventListener("click", function () {
          root.querySelectorAll(".candidate").forEach(function (c) {
            c.classList.remove("selected");
          });
          card.classList.add("selected");
          var idx = parseInt(card.getAttribute("data-cand"), 10);
          var c = candidates[idx];
          if (!c) return;
          var form = root.querySelector("[data-confirm-form]");
          if (!form) return;
          ["title", "author", "narrator", "series", "series_index", "year", "genre", "cover_url", "description", "asin"].forEach(
            function (field) {
              var input = form.querySelector('[name="' + field + '"]');
              if (input) input.value = c[field] || "";
            }
          );
        });
        card.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            card.click();
          }
        });
      });
    }

    var confirmForm = root.querySelector("[data-confirm-form]");
    if (confirmForm) {
      confirmForm.addEventListener("submit", function (e) {
        e.preventDefault();
        var jobId = confirmForm.getAttribute("data-job-id");
        post("/jobs/" + jobId + "/confirm", new FormData(confirmForm)).then(function () {
          refreshBoard(jobId);
        });
      });
    }

    root.querySelectorAll("[data-action]").forEach(function (btn) {
      var action = btn.getAttribute("data-action");
      if (action === "show-log") {
        btn.addEventListener("click", function () {
          var jobId = btn.getAttribute("data-job-id");
          var log = root.querySelector("#log-" + jobId);
          if (log) log.style.display = log.style.display === "none" ? "flex" : "none";
        });
        return;
      }
      if (["start", "cancel", "requeue", "remove"].indexOf(action) === -1) return;
      btn.addEventListener("click", function () {
        var jobId = btn.getAttribute("data-job-id");
        btn.disabled = true;
        post("/jobs/" + jobId + "/" + action)
          .then(function () {
            refreshBoard(jobId);
          })
          .finally(function () {
            btn.disabled = false;
          });
      });
    });
  }

  // ---- live progress + structural-change polling ----
  function pollStatus() {
    getJson("/api/status")
      .then(function (jobs) {
        var byId = {};
        jobs.forEach(function (j) {
          byId[j.id] = j;
        });

        updateLivePill(jobs);

        var structuralChange = false;
        var idsNow = Object.keys(byId);
        var idsBefore = Object.keys(knownStatuses);
        if (idsNow.length !== idsBefore.length) structuralChange = true;

        jobs.forEach(function (j) {
          var idStr = String(j.id);
          if (knownStatuses[idStr] !== undefined && knownStatuses[idStr] !== j.status) {
            structuralChange = true;
          }
          knownStatuses[idStr] = j.status;

          var chip = document.getElementById("q-status-" + j.id);
          if (chip && j.status === "processing") chip.textContent = j.progress_pct + "%";

          if (idStr === currentPanelId && j.status === "processing") {
            var pctLabel = document.getElementById("pctLabel-" + j.id);
            var waveFill = document.getElementById("waveFill-" + j.id);
            var stageLabel = document.getElementById("stageLabel-" + j.id);
            if (pctLabel) pctLabel.textContent = j.progress_pct + "%";
            if (waveFill) waveFill.style.width = j.progress_pct + "%";
            if (stageLabel && j.progress_stage) stageLabel.textContent = j.progress_stage;
          }

          if (j.status === "processing") {
            var ncPct = document.getElementById("ncPct");
            var ncFill = document.getElementById("ncFill");
            var ncStage = document.getElementById("ncStage");
            if (ncPct) ncPct.textContent = j.progress_pct + "%";
            if (ncFill) ncFill.style.width = j.progress_pct + "%";
            if (ncStage && j.progress_stage) ncStage.textContent = j.progress_stage;
          }
        });

        Object.keys(knownStatuses).forEach(function (id) {
          if (!(id in byId)) structuralChange = true;
        });

        if (structuralChange) {
          refreshBoard(currentPanelId);
        }
      })
      .catch(function () {
        /* transient network hiccup - just try again next tick */
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var activePanel = detail.querySelector(".panel[data-panel]");
    currentPanelId = activePanel ? activePanel.getAttribute("data-panel") : null;

    document.querySelectorAll(".queue-item").forEach(function (i) {
      knownStatuses[i.getAttribute("data-target")] = i.getAttribute("data-status");
    });

    wireRailInteractions();
    wireNowConverting();
    if (detail) {
      buildAllWaveforms(detail);
      wirePanelInteractions(detail);
    }
    buildAllWaveforms(document.querySelector(".topbar"));

    setInterval(pollStatus, 2500);
  });
})();
