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
// change. Every one of those DOM swaps (innerHTML/outerHTML/replaceWith) is
// wrapped in document.startViewTransition() when the browser supports it,
// so a real reload cross-fades instead of popping; browsers without the API
// just get today's instant swap (see withViewTransition()).
//
// pollStatus() is what notices background changes: it hits a cheap JSON
// endpoint every 2.5s, patches progress numbers in place for the common
// case (nothing but percent/stage changed), and falls back to a full
// refreshBoard() only when a job's *render group* changes (renderGroup()
// below - it collapses statuses that render identical HTML, like `queued`
// and `detecting`, so a purely-cosmetic backend transition between them
// doesn't trigger a reload) or the set of jobs changes.
//
// Every action button (start/cancel/requeue/remove/confirm/reorder)
// already triggers its own immediate refreshBoard() on click, which shows
// the up-to-date state right away. Since that bypasses pollStatus(), each
// of those handlers also calls syncKnownGroups() once the refresh settles,
// so the tracked baseline matches what's already on screen - otherwise the
// very next poll tick would compare against a stale pre-action group and
// force a redundant second reload of state the user already saw.
(function () {
  "use strict";

  var detail = document.getElementById("detail");
  var currentPanelId = null;
  var knownGroups = {}; // job id -> render group, used to detect structural changes while polling

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

  // Statuses that render byte-identical HTML in _panel.html/_queue_item.html
  // share one render group here, so a background transition between them
  // (e.g. queued -> detecting, which happens almost immediately once Huey
  // picks a job up) isn't treated as a change worth reloading for. Every
  // other status maps 1:1 to itself. Keep this in sync with the templates'
  // `{% elif job.status in (...) %}` grouping - it doesn't change what
  // renders, only what the poller considers "a change."
  var STATUS_RENDER_GROUP = {
    queued: "looking_up",
    detecting: "looking_up",
  };
  function renderGroup(status) {
    return STATUS_RENDER_GROUP[status] || status;
  }

  // Resyncs knownGroups to whatever's actually on screen right now, read
  // straight from the rail's data-status attributes. Called after the
  // DOMContentLoaded initial render and after every action-triggered
  // refreshBoard() settles, so the next pollStatus() tick compares against
  // the state the user already sees instead of a stale pre-action baseline.
  function syncKnownGroups() {
    var next = {};
    document.querySelectorAll(".queue-item").forEach(function (i) {
      next[i.getAttribute("data-target")] = renderGroup(i.getAttribute("data-status"));
    });
    knownGroups = next;
  }

  // Wraps a DOM-swapping callback in the View Transitions API when the
  // browser supports it, so the swap cross-fades instead of popping.
  // Browsers without the API just run the callback directly - today's
  // behavior, zero risk.
  //
  // The API only allows one active transition on the document at a time -
  // a second concurrent call aborts with an error instead of queueing. But
  // refreshBoard() below fans out to three swaps that land close together
  // (rail + now-converting concurrently, then the panel right after), so a
  // naive per-call wrap here would collide with itself on every reload.
  // `inTransition` is how refreshBoard() tells this function "there's
  // already an outer transition running - just apply the change inline, it
  // will be captured as part of that one" instead of starting a nested one.
  //
  // The browser can also abort a transition outright - most commonly
  // because the document is hidden (pollStatus() keeps polling, and can
  // trigger a reload, in a backgrounded tab) - which rejects its `ready`
  // promise. The swap itself (run synchronously either way) still applies
  // either way; only the animation is skipped. settleTransition() just
  // keeps that rejection from surfacing as an uncaught promise error.
  var inTransition = false;
  function settleTransition(transition) {
    transition.ready.catch(function () {});
    transition.finished.catch(function () {});
    return transition;
  }
  // Returns a promise that resolves only once `swap` has actually run and
  // its DOM changes are applied - callers MUST chain any post-swap work
  // (rewiring listeners, rebuilding waveforms) off this promise rather than
  // running it right after calling withViewTransition(). When the browser
  // supports the View Transitions API, document.startViewTransition()'s
  // callback runs asynchronously (not in the same task), so code that
  // assumed `swap` had already applied by the time the call returned would
  // silently wire listeners onto the stale, about-to-be-replaced DOM
  // instead of the fresh nodes the swap installs a moment later.
  function withViewTransition(swap) {
    if (inTransition || typeof document.startViewTransition !== "function") {
      swap();
      return Promise.resolve();
    }
    var transition = settleTransition(document.startViewTransition(swap));
    return transition.updateCallbackDone;
  }

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
      return withViewTransition(function () {
        detail.innerHTML = html;
      }).then(function () {
        currentPanelId = String(jobId);
        buildAllWaveforms(detail);
        wirePanelInteractions(detail);
      });
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
      return withViewTransition(function () {
        var rail = document.getElementById("rail");
        rail.outerHTML = html;
      }).then(function () {
        wireRailInteractions();
        var activeItem = document.querySelector(".queue-item.active");
        if (activeItem) activeItem.classList.add("active");
      });
    });
  }

  function refreshNowConverting() {
    return getHtml("/fragments/now-converting").then(function (html) {
      var wrapper = document.createElement("div");
      wrapper.innerHTML = html;
      var next = wrapper.firstElementChild;
      var current = document.getElementById("nowConverting");
      if (current && next) {
        return withViewTransition(function () {
          current.replaceWith(next);
        }).then(function () {
          buildAllWaveforms(document.querySelector(".topbar"));
          wireNowConverting();
        });
      }
    });
  }

  function refreshBoard(focusJobId) {
    // Rail, now-converting, and (usually) the panel all swap together here.
    // Run the whole sequence as ONE view transition rather than letting
    // each swap above start its own - see the comment on withViewTransition
    // for why three concurrent ones would collide - so a real reload
    // cross-fades every region at once instead of three racing animations.
    function run() {
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
        return withViewTransition(function () {
          detail.innerHTML =
            '<div class="card"><p class="waiting-note">Nothing here yet. Drop an audiobook into the inbox folder to get started.</p></div>';
        }).then(function () {
          currentPanelId = null;
        });
      });
    }

    if (inTransition || typeof document.startViewTransition !== "function") {
      return run();
    }
    inTransition = true;
    var transition = settleTransition(
      document.startViewTransition(function () {
        return run().finally(function () {
          inTransition = false;
        });
      })
    );
    return transition.updateCallbackDone;
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
          return refreshBoard(currentPanelId).then(syncKnownGroups);
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

  // ---- candidate cards (selecting one fills in the confirm form below) ----
  // Pulled out of wirePanelInteractions so a manual search (see the
  // data-search-form handler below) can re-wire just the swapped-in
  // candidates fragment without re-binding the confirm form/action buttons
  // a second time.
  function wireCandidateInteractions(root, candidatesEl) {
    var candidates = [];
    try {
      candidates = JSON.parse(candidatesEl.getAttribute("data-candidates-json"));
    } catch (e) {
      candidates = [];
    }
    // Fields the confirm form can be populated from. Kept as one list so the
    // "None of these" row below and a real candidate pick always write the
    // exact same set of fields - no field left holding a stale value from a
    // previously-selected candidate.
    var fields = ["title", "author", "narrator", "series", "series_index", "year", "genre", "cover_url", "description", "asin"];
    var titleGuess = candidatesEl.getAttribute("data-title-guess") || "";
    var authorGuess = candidatesEl.getAttribute("data-author-guess") || "";
    var cards = candidatesEl.querySelectorAll(".candidate");
    cards.forEach(function (card) {
      card.setAttribute("tabindex", "0");
      card.setAttribute("role", "radio");
      card.addEventListener("click", function () {
        cards.forEach(function (c) {
          c.classList.remove("selected");
        });
        card.classList.add("selected");
        var form = root.querySelector("[data-confirm-form]");
        if (!form) return;
        // "None of these" - reset to the same no-candidate-chosen state the
        // empty-candidates-list case already gets server-side: title/author
        // fall back to the job's guesses, everything else (asin included)
        // goes blank, rather than leaving a previous selection's values in
        // place.
        if (card.classList.contains("candidate-none")) {
          fields.forEach(function (field) {
            var input = form.querySelector('[name="' + field + '"]');
            if (!input) return;
            if (field === "title") input.value = titleGuess;
            else if (field === "author") input.value = authorGuess;
            else input.value = "";
          });
          return;
        }
        var idx = parseInt(card.getAttribute("data-cand"), 10);
        var c = candidates[idx];
        if (!c) return;
        fields.forEach(function (field) {
          var input = form.querySelector('[name="' + field + '"]');
          if (input) input.value = c[field] || "";
        });
      });
      card.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          card.click();
        }
      });
    });
  }

  // ---- panel interactions (candidates, search, confirm form, action buttons) ----
  function wirePanelInteractions(root) {
    var candidatesEl = root.querySelector(".candidates[data-candidates-json]");
    if (candidatesEl) {
      wireCandidateInteractions(root, candidatesEl);
    }

    var searchForm = root.querySelector("[data-search-form]");
    if (searchForm) {
      searchForm.addEventListener("submit", function (e) {
        e.preventDefault();
        var jobId = searchForm.getAttribute("data-job-id");
        var submitBtn = searchForm.querySelector("button[type=submit]");
        if (submitBtn) submitBtn.disabled = true;
        post("/jobs/" + jobId + "/search", new FormData(searchForm))
          .then(function (res) {
            return res.text();
          })
          .then(function (html) {
            var old = root.querySelector("#candidatesWrap");
            if (!old) return;
            var wrapper = document.createElement("div");
            wrapper.innerHTML = html;
            var next = wrapper.firstElementChild;
            old.replaceWith(next);
            wireCandidateInteractions(root, next);
            // Mirror the initial automatic search: the top new result is
            // pre-selected, so click it to carry those fields (asin
            // included) into the confirm form below, same as a manual click.
            var firstCard = next.querySelector(".candidate");
            if (firstCard) firstCard.click();
          })
          .finally(function () {
            if (submitBtn) submitBtn.disabled = false;
          });
      });
    }

    var confirmForm = root.querySelector("[data-confirm-form]");
    if (confirmForm) {
      confirmForm.addEventListener("submit", function (e) {
        e.preventDefault();
        var jobId = confirmForm.getAttribute("data-job-id");
        post("/jobs/" + jobId + "/confirm", new FormData(confirmForm)).then(function () {
          return refreshBoard(jobId).then(syncKnownGroups);
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
            return refreshBoard(jobId).then(syncKnownGroups);
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
        var idsBefore = Object.keys(knownGroups);
        if (idsNow.length !== idsBefore.length) structuralChange = true;

        jobs.forEach(function (j) {
          var idStr = String(j.id);
          var group = renderGroup(j.status);
          if (knownGroups[idStr] !== undefined && knownGroups[idStr] !== group) {
            structuralChange = true;
          }
          knownGroups[idStr] = group;

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

        Object.keys(knownGroups).forEach(function (id) {
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

    syncKnownGroups();

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
