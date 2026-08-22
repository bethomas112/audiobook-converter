# Frontend Action Error Toasts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every POST action the web UI triggers (start / cancel / requeue /
remove / confirm / reorder / search) must show the user a visible error
message when the request fails, instead of silently doing nothing.

**Architecture:** A single shared `showErrorToast(message)` helper in
`app/web/static/app.js`, styled to match the app's existing warm/rust
error palette (the same colors `.st-failed` already uses), and a small
enhancement to the shared `post()` fetch helper so it extracts the
server's actual error text (FastAPI's `{"detail": "..."}` body) instead of
discarding it. Every POST call site that's currently missing a `.catch()`
gets one, calling the toast. One adjacent fix: the confirm form's submit
button currently isn't disabled during its own request (every other
action button already does this) — added for consistency.

**Tech Stack:** Vanilla JS (no framework, no build step — see
`app/web/static/app.js`'s own header comment), plain CSS
(`app/web/static/style.css`). No JS test framework exists in this repo
(confirmed: no `package.json`, no JS test runner) — verification is via a
real running server and a real browser, not automated unit tests.

**Spec:** None — bounded-scope fix to an existing file, design worked out
interactively with Brady and approved in chat. This plan is self-contained.

## Global Constraints

- **Reuse the app's existing color tokens.** Errors use `--rust` /
  `--rust-wash` (defined in `style.css`'s `:root` block) — the same colors
  already used for a failed job's status pill (`.st-failed`). Do not
  introduce a new red/error color.
- **Match existing typography conventions.** Mono, uppercase, letter-spaced
  labels (see `.eyebrow`, `.rail-header-label`) for a small label; the
  body font (`--font-body`) for the message text itself.
- **No new template changes.** The toast container is created and
  appended to `document.body` from JavaScript — `index.html` and the
  Jinja partials are not touched.
- **Every failure message must come from the server when the server gives
  one, and fall back to a generic message otherwise** — never a blank or
  raw technical string like `"Request failed: /jobs/5/cancel"`.

---

## Task 1: Toast infrastructure, wired into every POST action

**Files:**
- Modify: `app/web/static/style.css`
- Modify: `app/web/static/app.js`

**Interfaces:** None consumed (first task touching this area). Produces
nothing other tasks depend on — this plan has only one task.

- [ ] **Step 1: Add toast CSS**

Add this block to `app/web/static/style.css`, anywhere after the `:root`
block (e.g. at the end of the file, after the existing `@keyframes
fadeOut{...}` on line 392):

```css
/* ---- error toasts (network/action failures) ---- */
.toast-stack{
  position:fixed; bottom:1.4rem; right:1.4rem; z-index:100;
  display:flex; flex-direction:column; gap:0.5rem; align-items:flex-end;
  pointer-events:none;
}
.toast{
  pointer-events:auto; max-width:22rem; cursor:pointer;
  background:var(--surface-2); border:1px solid var(--hairline-strong);
  border-left:3px solid var(--rust); border-radius:10px;
  box-shadow:0 8px 24px rgba(0,0,0,0.35);
  padding:0.7rem 0.9rem;
  animation:toastIn 0.2s ease;
}
.toast-eyebrow{
  font-family:var(--font-mono); font-size:0.66rem; letter-spacing:0.1em;
  text-transform:uppercase; color:var(--rust); display:block; margin-bottom:0.15rem;
}
.toast-message{font-family:var(--font-body); font-size:0.85rem; color:var(--paper); line-height:1.35;}
@keyframes toastIn{from{opacity:0; transform:translateY(6px);} to{opacity:1; transform:none;}}
@media (prefers-reduced-motion: reduce){
  .toast{animation:none;}
}
```

- [ ] **Step 2: Add the `showErrorToast` helper and enhance `post()`**

In `app/web/static/app.js`, replace the existing `post()` function
(currently lines 135-140):

```js
  function post(url, formData) {
    return fetch(url, { method: "POST", body: formData || new FormData() }).then(function (res) {
      if (!res.ok) throw new Error("Request failed: " + url);
      return res;
    });
  }
```

with this (the fetch/response-checking behavior for a SUCCESSFUL request
is unchanged; only the failure path now extracts the server's real error
text):

```js
  // ---- error toasts (network/action failures) ----
  var TOAST_DURATION_MS = 5000;
  function getToastStack() {
    var stack = document.getElementById("toastStack");
    if (!stack) {
      stack = document.createElement("div");
      stack.id = "toastStack";
      stack.className = "toast-stack";
      document.body.appendChild(stack);
    }
    return stack;
  }
  function showErrorToast(message) {
    var stack = getToastStack();
    var toast = document.createElement("div");
    toast.className = "toast";
    var eyebrow = document.createElement("span");
    eyebrow.className = "toast-eyebrow";
    eyebrow.textContent = "Error";
    var body = document.createElement("span");
    body.className = "toast-message";
    body.textContent = message;
    toast.appendChild(eyebrow);
    toast.appendChild(body);
    toast.addEventListener("click", function () {
      toast.remove();
    });
    stack.appendChild(toast);
    setTimeout(function () {
      toast.remove();
    }, TOAST_DURATION_MS);
  }

  function post(url, formData) {
    return fetch(url, { method: "POST", body: formData || new FormData() }).then(function (res) {
      if (!res.ok) {
        // FastAPI's HTTPException responses are {"detail": "..."} - surface
        // that real message (e.g. the search route's "Enter a title or
        // author to search.") rather than a generic failure string. Falls
        // back to a generic message if the body isn't JSON or has no
        // usable detail (e.g. a plain 500 with an HTML error page).
        return res
          .json()
          .catch(function () {
            return null;
          })
          .then(function (body) {
            var message = (body && body.detail) || "Something went wrong - try again.";
            throw new Error(message);
          });
      }
      return res;
    });
  }
```

Place this new block where `post()` currently is (lines 135-140) - i.e.
`showErrorToast`/`getToastStack`/`TOAST_DURATION_MS` go immediately
before the replaced `post()` function, in the same spot.

- [ ] **Step 3: Wire `.catch()` into the reorder buttons**

In `app/web/static/app.js`, inside `wireRailInteractions()`, find (around
line 302-314):

```js
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
```

Replace the `post(...)` call's promise chain with:

```js
        post("/jobs/" + jobId + "/reorder", fd)
          .then(function () {
            return refreshBoard(currentPanelId).then(syncKnownGroups);
          })
          .catch(function (e) {
            showErrorToast(e.message);
          });
```

- [ ] **Step 4: Wire `.catch()` into the metadata search form**

In `app/web/static/app.js`, inside `wirePanelInteractions()`, find (around
line 396-425):

```js
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
```

Insert a `.catch()` immediately before the existing `.finally()`:

```js
          .catch(function (e) {
            showErrorToast(e.message);
          })
          .finally(function () {
            if (submitBtn) submitBtn.disabled = false;
          });
```

(Only the `.catch()` block is new; the `.finally()` block and everything
above it is unchanged.)

- [ ] **Step 5: Wire `.catch()` and a disable/enable guard into the confirm form**

In `app/web/static/app.js`, inside `wirePanelInteractions()`, find (around
line 427-436):

```js
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
```

Replace with:

```js
    var confirmForm = root.querySelector("[data-confirm-form]");
    if (confirmForm) {
      confirmForm.addEventListener("submit", function (e) {
        e.preventDefault();
        var jobId = confirmForm.getAttribute("data-job-id");
        var submitBtn = confirmForm.querySelector("button[type=submit]");
        if (submitBtn) submitBtn.disabled = true;
        post("/jobs/" + jobId + "/confirm", new FormData(confirmForm))
          .then(function () {
            return refreshBoard(jobId).then(syncKnownGroups);
          })
          .catch(function (e) {
            showErrorToast(e.message);
          })
          .finally(function () {
            if (submitBtn) submitBtn.disabled = false;
          });
      });
    }
```

(This adds the same disable-during-request/re-enable-after pattern every
other action already uses — `_panel.html` line 84 confirms the confirm
form's submit button is `<button type="submit" class="btn btn-primary">`,
so `confirmForm.querySelector("button[type=submit]")` matches it, same
selector the search form already uses.)

- [ ] **Step 6: Wire `.catch()` into the shared start/cancel/requeue/remove handler**

In `app/web/static/app.js`, inside `wirePanelInteractions()`, find (around
line 438-460):

```js
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
```

Insert a `.catch()` immediately before the existing `.finally()`:

```js
          .catch(function (e) {
            showErrorToast(e.message);
          })
          .finally(function () {
            btn.disabled = false;
          });
```

(Only the `.catch()` block is new.)

- [ ] **Step 7: Set up a live server with test data for browser verification**

Create a throwaway setup script at `/tmp/toast_test_setup.py` (outside
the repo - this is scratch, not part of the plan's deliverable) with this
exact content:

```python
import os
import tempfile
from pathlib import Path

base = Path(tempfile.mkdtemp(prefix="toast-test-"))
for sub in ("inbox", "work", "archive", "output", "config"):
    (base / sub).mkdir(parents=True, exist_ok=True)

os.environ["INBOX_DIR"] = str(base / "inbox")
os.environ["WORK_DIR"] = str(base / "work")
os.environ["ARCHIVE_DIR"] = str(base / "archive")
os.environ["OUTPUT_DIR"] = str(base / "output")
os.environ["CONFIG_DIR"] = str(base / "config")

import sys
sys.path.insert(0, "REPO_ROOT_PLACEHOLDER")

from app.db import Job, init_db

init_db()

# Job 1: awaiting_metadata_confirm, blank guesses, no candidates - the
# search form starts with both title/author fields empty, so clicking
# "Search" with no edits naturally hits the server's real 400 validation
# ("Enter a title or author to search.") with zero DOM manipulation needed.
Job.create(
    source_path=str(base / "inbox" / "Blank Guess Book.mp3"),
    status=Job.STATUS_AWAITING_METADATA_CONFIRM,
    source_type="mp3_single",
    audio_files_json='["' + str(base / "inbox" / "Blank Guess Book.mp3") + '"]',
    title_guess="",
    author_guess="",
    candidates_json="[]",
)

# Job 2: ready, for testing the click-handler action-button path (e.g.
# "cancel") - not the form-submit path Job 1 covers.
Job.create(
    source_path=str(base / "inbox" / "Ready Book.mp3"),
    status=Job.STATUS_READY,
    source_type="mp3_single",
    audio_files_json='["' + str(base / "inbox" / "Ready Book.mp3") + '"]',
    title_guess="Ready Book",
    author_guess="Some Author",
    selected_metadata_json='{"title": "Ready Book", "author": "Some Author", "asin": "", "narrator": "", "series": "", "series_index": "", "year": "", "genre": "", "description": "", "cover_url": ""}',
    queue_order=1,
)

print("CONFIG_DIR=" + str(base / "config"))
print("Setup complete. Job 1 (search 400 test) and Job 2 (button 404 test) created.")
```

Replace `REPO_ROOT_PLACEHOLDER` with this repo's actual absolute root path
before running it. Run it with the repo's `.venv`:

```bash
.venv/bin/python /tmp/toast_test_setup.py
```

Note the `CONFIG_DIR=...` path it prints - the server needs the exact
same `CONFIG_DIR`/`INBOX_DIR`/etc. values to see the jobs you just
created. Start the server pointed at them (adjust the paths to match what
the script printed and created - they all share the same `base` temp
directory, e.g. `CONFIG_DIR=/tmp/toast-test-xxxxx/config`,
`INBOX_DIR=/tmp/toast-test-xxxxx/inbox`, etc.):

```bash
INBOX_DIR=/tmp/toast-test-xxxxx/inbox WORK_DIR=/tmp/toast-test-xxxxx/work ARCHIVE_DIR=/tmp/toast-test-xxxxx/archive OUTPUT_DIR=/tmp/toast-test-xxxxx/output CONFIG_DIR=/tmp/toast-test-xxxxx/config .venv/bin/uvicorn app.main:app --port 8099
```

(The Huey consumer isn't needed for this verification - neither test job
requires a real conversion to run.)

- [ ] **Step 8: Browser-verify the natural 400 case (search form)**

Using the browser tool: navigate to `http://localhost:8099`. Job 1
("Blank Guess Book") should be selected/visible in the "Needs Input" rail
with its review panel open, title and author fields both blank. Click the
"Search" button without typing anything.

Expected: a toast appears in the bottom-right with the eyebrow "Error" and
the message "Enter a title or author to search." (the server's real
validation message, extracted via the new `post()` logic) - not a silent
no-op, not a generic "Something went wrong" (that fallback text is only
for responses with no usable `detail`, and this one has one). Take a
screenshot confirming the toast is visible and styled (rust-colored left
border, readable text, positioned bottom-right, doesn't block the rest of
the page). Click the toast and confirm it dismisses.

- [ ] **Step 9: Browser-verify a forced 404 on the click-handler path (action button)**

Still in the browser: select Job 2 ("Ready Book") in the rail. Its panel
shows a "Take out of queue" button (`data-action="cancel"`). Using the
browser's JS execution tool, run this against the live page to force a
404 without touching any server code (this exercises the real,
unmodified click handler against a job id that doesn't exist - not a
mock):

```js
document.querySelector('[data-action="cancel"]').setAttribute('data-job-id', '999999');
```

Then click that same "Take out of queue" button.

Expected: a toast appears with the message "Not Found" (FastAPI's default
`detail` text for a bare `HTTPException(status_code=404)` - see
`app/web/routes.py`'s `cancel` route, which raises exactly that with no
custom detail). Screenshot confirming it. Also confirm the button itself
re-enabled afterward (not stuck `disabled`) - the existing `.finally()`
still runs after the new `.catch()`.

- [ ] **Step 10: Self-review the remaining wiring points**

Re-read the full diff of `app/web/static/app.js`. Confirm by inspection
(reorder and the shared start/cancel/requeue/remove handler share the
exact same `post().then().catch().finally()` shape already
browser-verified above in Steps 8-9, just against different endpoints, so
a code-level check is the appropriate verification here rather than
repeating the same browser test four more times):
- The reorder buttons' `.catch()` (Step 3) is positioned correctly -
  between the existing `.then()` and the end of the chain, not swallowing
  the `refreshBoard`/`syncKnownGroups` call.
- No `.catch()` was accidentally left off any of the four wiring points.
- `showErrorToast` and `getToastStack` are defined before first use (i.e.
  above `post()` in file order, since `post()`'s Step 2 replacement
  references `showErrorToast` only indirectly via callers - actually
  `post()` itself never calls `showErrorToast`, only its callers do, so
  definition order only matters at call time, which is safely after
  `DOMContentLoaded` - but confirm this reasoning holds by checking the
  actual file).

- [ ] **Step 11: Clean up the throwaway test server and script**

Stop the `uvicorn` process (e.g. `Ctrl+C` in its terminal, or `kill` the
PID). Delete `/tmp/toast_test_setup.py` and the `/tmp/toast-test-xxxxx`
temp directory it created - none of this is part of the repo or the
commit.

- [ ] **Step 12: Commit**

```bash
git add app/web/static/app.js app/web/static/style.css
git commit -m "feat: surface a toast when a job action request fails"
```

---

## Self-Review Notes

- **Spec coverage:** all four missing `.catch()` points (reorder, search,
  confirm, start/cancel/requeue/remove) are covered (Steps 3-6); the
  server-detail-extraction requirement is covered (Step 2); the
  confirm-button disable/enable gap is covered (Step 5); the "use existing
  color tokens" constraint is covered (Step 1, uses `--rust`/`--rust-wash`
  exclusively, no new colors).
- **No placeholders:** every step has complete, exact code - the one
  literal placeholder (`REPO_ROOT_PLACEHOLDER` in the throwaway test
  script) is explicitly flagged as something the implementer must replace
  before running, not an unfinished plan step.
- **Type/signature consistency:** `showErrorToast(message)` is defined
  once (Step 2) and called identically (`showErrorToast(e.message)`) at
  all four wiring points (Steps 3-6) - no naming drift.
