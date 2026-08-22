# Frontend Stale Connection Indicator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the browser's 2.5s status poll starts failing (server
unreachable — container restart, network drop), the user must see a
visible "can't reach server" indicator instead of the UI silently going
stale with no sign anything is wrong.

**Architecture:** Reuse the existing top-bar live-status pill
(`#livePill`/`#livePillText`) rather than adding a new UI element. After
3 consecutive poll failures (~7.5s — long enough to ignore a single
dropped request, short enough to notice a real outage quickly), flip the
pill into an "offline" visual state: its pulsing dot recolors from amber
to the same `--rust` used for errors, and its text swaps to a plain
"can't reach server" message. The moment a poll succeeds again, it
silently reverts to normal — no separate "reconnected" message needed.

**Tech Stack:** Vanilla JS (`app/web/static/app.js`), plain CSS
(`app/web/static/style.css`) — same no-framework, no-build-step
constraints as the rest of this app. No JS test framework exists in this
repo — verification is a real running server and a real browser, using a
`window.fetch` override to deterministically simulate a network failure
(this exercises the actual `pollStatus()` code path for real, since
`getJson()` calls the browser's real `fetch` — it isn't a mock of this
app's own code, only of the network layer underneath it).

**Spec:** None — bounded-scope fix to existing code, design worked out
interactively with Brady and approved in chat. This plan is
self-contained.

## Global Constraints

- **No new UI element.** The existing `#livePill`/`#livePillText` in the
  top bar is reused via a state class (`.offline`), not a new banner,
  toast, or modal.
- **Reuse existing color tokens.** Offline state uses `--rust` /
  `--rust-wash` (already defined in `style.css`'s `:root` block, already
  used for `.st-failed` and the error toasts from the prior task) — no
  new color values.
- **Threshold is exactly 3 consecutive failures** before showing the
  offline state — not 1 (too flickery on a single dropped request), not
  more (too slow to notice a real outage).
- **Recovery is silent.** No "reconnected" toast or message — the pill
  returning to its normal amber/counts state on the next successful poll
  is the only signal, matching this app's existing restrained tone.
- **This is NOT the same mechanism as the error toasts** added in the
  prior task (`docs/superpowers/plans/2026-08-21-frontend-action-error-toasts.md`).
  Do not fire a toast on every failed poll — that would be one every 2.5s
  during an outage, which is spammy. A persistent status-pill state is
  the correct shape for an ongoing condition; a toast is for a one-off
  event.

---

## Task 1: Offline state on the live-status pill

**Files:**
- Modify: `app/web/static/style.css`
- Modify: `app/web/static/app.js`

**Interfaces:** None consumed. Produces nothing other tasks depend on —
this plan has only one task.

- [ ] **Step 1: Add the offline-state CSS**

In `app/web/static/style.css`, find the `.live-dot` rule and its
`@keyframes pulse` block (currently lines 83-90):

```css
.live-dot{width:7px; height:7px; border-radius:50%; background:var(--amber); box-shadow:0 0 0 3px var(--amber-wash); flex:none;}
@media (prefers-reduced-motion: no-preference){
  .live-dot{animation:pulse 2.2s ease-in-out infinite;}
}
@keyframes pulse{
  0%,100%{opacity:1; box-shadow:0 0 0 3px var(--amber-wash);}
  50%{opacity:0.55; box-shadow:0 0 0 6px transparent;}
}
```

Replace it with (this introduces one CSS custom property,
`--pulse-wash`, defaulting to the existing `--amber-wash` via the
fallback in `var(--pulse-wash, var(--amber-wash))` — so normal behavior
is byte-for-byte unchanged unless something sets `--pulse-wash`
explicitly, which only the new `.offline` rule below does):

```css
.live-dot{
  width:7px; height:7px; border-radius:50%; background:var(--amber);
  box-shadow:0 0 0 3px var(--pulse-wash, var(--amber-wash)); flex:none;
}
@media (prefers-reduced-motion: no-preference){
  .live-dot{animation:pulse 2.2s ease-in-out infinite;}
}
@keyframes pulse{
  0%,100%{opacity:1; box-shadow:0 0 0 3px var(--pulse-wash, var(--amber-wash));}
  50%{opacity:0.55; box-shadow:0 0 0 6px transparent;}
}
.live-pill.offline .live-dot{background:var(--rust); --pulse-wash:var(--rust-wash);}
```

- [ ] **Step 2: Add the failure-counter and offline-toggle logic**

In `app/web/static/app.js`, find the module-level variable declarations
near the top of the IIFE (currently lines 36-38):

```js
  var detail = document.getElementById("detail");
  var currentPanelId = null;
  var knownGroups = {}; // job id -> render group, used to detect structural changes while polling
```

Add one more variable immediately after `knownGroups`:

```js
  var detail = document.getElementById("detail");
  var currentPanelId = null;
  var knownGroups = {}; // job id -> render group, used to detect structural changes while polling
  var consecutivePollFailures = 0; // reset on any successful poll; drives the offline pill state below
```

Then, immediately after the existing `updateLivePill()` function
(currently ending at line 133 with its closing `}`), add:

```js

  // ---- stale-connection indicator ----
  // 3 consecutive poll failures (~7.5s at the 2.5s poll interval) before
  // showing anything - long enough that a single dropped request doesn't
  // flicker the pill, short enough to notice a real outage quickly.
  var POLL_FAILURE_THRESHOLD = 3;
  function setConnectionOffline(offline) {
    var pill = document.getElementById("livePill");
    var pillText = document.getElementById("livePillText");
    if (!pill || !pillText) return;
    pill.classList.toggle("offline", offline);
    if (offline) {
      pillText.textContent = "Can't reach server - retrying...";
    }
    // Recovery text is NOT set here - the next successful poll's
    // updateLivePill() call unconditionally overwrites pillText with the
    // real counts, which is what "recovered" should show anyway.
  }
```

- [ ] **Step 3: Wire the counter into `pollStatus()`**

In `app/web/static/app.js`, find `pollStatus()` (currently around lines
542-598). It currently reads:

```js
  function pollStatus() {
    getJson("/api/status")
      .then(function (jobs) {
        var byId = {};
        jobs.forEach(function (j) {
          byId[j.id] = j;
        });

        updateLivePill(jobs);

        var structuralChange = false;
        ... (unchanged)
      })
      .catch(function () {
        /* transient network hiccup - just try again next tick */
      });
  }
```

Change only the very start of the `.then()` callback and the entire
`.catch()` callback:

```js
  function pollStatus() {
    getJson("/api/status")
      .then(function (jobs) {
        consecutivePollFailures = 0;
        setConnectionOffline(false);

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
        // transient network hiccup - tolerate a couple of misses before
        // telling the user anything; see POLL_FAILURE_THRESHOLD above.
        consecutivePollFailures++;
        if (consecutivePollFailures >= POLL_FAILURE_THRESHOLD) {
          setConnectionOffline(true);
        }
      });
  }
```

Only three things changed from the current code: the two new lines at
the top of `.then()` (`consecutivePollFailures = 0;` and
`setConnectionOffline(false);`), and the entire body of `.catch()`
(previously just a comment, now the counter/threshold logic). Every other
line above is copied verbatim from the file's current content — replace
the whole function with the block above rather than trying to patch it
piecemeal, to avoid drift.

- [ ] **Step 4: Set up a live server for browser verification**

No database seeding is needed this time (unlike the prior toast task) —
the live pill's normal state ("0 converting/queued · 0 need input") is
valid with zero jobs. Start the server against a fresh empty temp
environment:

```bash
mkdir -p /tmp/stale-poll-test/{inbox,work,archive,output,config}
INBOX_DIR=/tmp/stale-poll-test/inbox WORK_DIR=/tmp/stale-poll-test/work ARCHIVE_DIR=/tmp/stale-poll-test/archive OUTPUT_DIR=/tmp/stale-poll-test/output CONFIG_DIR=/tmp/stale-poll-test/config .venv/bin/uvicorn app.main:app --port 8099
```

(No Huey consumer needed - nothing in this task requires a real
conversion to run.)

- [ ] **Step 5: Browser-verify the offline state appears**

Using the browser tool: navigate to `http://localhost:8099`. Confirm the
live pill shows its normal state first (amber dot, "0 converting/queued
· 0 need input").

Then use the browser's JS execution tool to force every `/api/status`
poll to fail, without touching this app's own code — this exercises the
real `pollStatus()`/`getJson()`/`fetch()` path for a genuine network
failure, not a mock of this app's logic:

```js
window.__origFetch = window.fetch;
window.fetch = function (url, opts) {
  if (String(url).indexOf("/api/status") !== -1) {
    return Promise.reject(new TypeError("Failed to fetch"));
  }
  return window.__origFetch(url, opts);
};
```

Wait at least 9 seconds (3 failed polls at 2.5s apart, plus a margin),
then screenshot the top bar. Expected: the live pill's dot is now
rust-colored (not amber) and its text reads exactly "Can't reach server -
retrying...".

- [ ] **Step 6: Browser-verify recovery**

Restore real `fetch`:

```js
window.fetch = window.__origFetch;
delete window.__origFetch;
```

Wait at least 3 seconds (one more poll interval), then screenshot the top
bar again. Expected: the live pill is back to its normal amber dot and
"0 converting/queued · 0 need input" text - no leftover "offline"
styling, no manual reset needed.

- [ ] **Step 7: Clean up the throwaway test server**

Stop the `uvicorn` process. Delete `/tmp/stale-poll-test/`. None of this
is part of the repo or the commit.

- [ ] **Step 8: Commit**

```bash
git add app/web/static/app.js app/web/static/style.css
git commit -m "feat: show a status-pill indicator when the background poll can't reach the server"
```

---

## Self-Review Notes

- **Spec coverage:** the 3-failure threshold (Global Constraint) is
  covered in Step 2/3; reuse of the existing pill with no new UI element
  is covered throughout (no template changes anywhere in this plan);
  silent recovery (no "reconnected" message) is covered in Step 2's
  `setConnectionOffline(false)` (it only ever sets text on the `offline`
  branch, deliberately leaving recovery text to `updateLivePill()`'s
  already-existing unconditional overwrite); reuse of `--rust`/
  `--rust-wash` is covered in Step 1; the "not the same mechanism as
  toasts" constraint is satisfied by construction — nothing in this plan
  calls `showErrorToast`.
- **No placeholders:** every step has complete, exact code. The one `...`
  in Step 3's code block is explicitly called out as non-literal, with
  precise instructions for what goes there (the existing unmodified
  function body), not an unfinished step.
- **Type/signature consistency:** `setConnectionOffline(offline)` is
  defined once (Step 2) and called exactly twice, both in Step 3
  (`setConnectionOffline(false)` on success, `setConnectionOffline(true)`
  past the threshold on failure) — no naming drift.
