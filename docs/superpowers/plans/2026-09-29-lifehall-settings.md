# Lifehall Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Lifehall edition a settings page with store-code entry, email and WeCom channel configuration, and only the business push switches that Lifehall actually supports.

**Architecture:** Extend the existing `general` footer page in the single edition page allowlist. Keep the existing store-code endpoint and channel configuration APIs, mount one store-code form in the login step and another in the Lifehall settings page, and filter notification preferences at the backend edition boundary so old full-edition switches cannot be displayed or changed.

**Tech Stack:** Python 3.14, pytest, native JavaScript, HTML/CSS, existing HTTP JSON routes and `.secrets` configuration.

---

### Task 1: Lock the settings and edition contract with failing tests

**Files:**
- Modify: `tests/test_edition.py`
- Modify: `tests/test_edition_api.py`
- Modify: `tests/test_web.py`

- [x] Assert `general` is present in `LIFEHALL_PAGES`, `badge` is absent, and the Lifehall HTML has both login and settings store-code hosts plus existing email/WeCom editors.
- [x] Exercise `GET /api/notify-pref` in Lifehall and assert no returned `kind == "feature"` keys exist today, while mail and WeCom platform config remain present.
- [x] Exercise `PUT /api/notify-pref` with a legacy feature key such as `attain`; expect HTTP 400 in Lifehall and unchanged saved preferences.
- [x] Run the focused edition and web tests first; confirmed the new settings assertions failed before implementation.

### Task 2: Implement Lifehall settings and both store-code entry points

**Files:**
- Modify: `src/edition.py`
- Modify: `web/index.html`
- Modify: `web/app.js`
- Modify: `web/style.css`
- Test: `tests/test_edition.py`, `tests/test_web.py`

- [x] Add only the `general` settings page to `LIFEHALL_PAGES`; leave the current Lifehall tool list (including the hidden badge) as approved.
- [x] Put a Lifehall-only store-code host in the login step and another in `subpanel-general`; use class/data selectors rather than duplicate IDs and send both forms to `PUT /api/session/store-code`.
- [x] After a successful save, refresh `setupState.profile.huawei_code`, synchronize all mounted store-code inputs, and refresh the overview only if the setup state is ready.
- [x] Label the Lifehall footer entry as settings, show mail and WeCom configuration there, and hide full-edition-only service/timer prose in the Lifehall view. Skip the full-edition account/service loaders in Lifehall. Preserve the full edition labels and cards.
- [x] Verify both hosts contain a usable editor before login and after opening settings, using wiring tests rather than a browser-only assumption.

### Task 3: Filter push preferences at the Lifehall API boundary

**Files:**
- Modify: `src/web.py`
- Modify: `web/app.js`
- Modify: `web/index.html`
- Test: `tests/test_edition_api.py`, `tests/test_web.py`

- [x] On `GET /api/notify-pref`, Lifehall returns only channel-level preferences until a retained Lifehall feature actually calls the notification service.
- [x] On `PUT /api/notify-pref`, Lifehall accepts the channel keys that the UI supports and rejects non-Lifehall feature keys with HTTP 400; leave full-edition preference handling unchanged.
- [x] Rename the Lifehall card to “业务推送” and show “生活馆版当前没有单独配置的业务推送项” when no actual Lifehall feature push exists.
- [x] Run the focused API and frontend wiring tests and confirm the full edition tests still assert their original preference behavior.

### Task 4: Synchronize design docs and verify the branch

**Files:**
- Modify: `.dsh/docs/2026-09-26-生活馆版-开发目标.md`
- Modify: `.dsh/docs/2026-09-27-生活馆抓取超时-诊断与修复-开发目标.md`
- Modify: `.dsh/docs/2026-09-29-2.2.0-lifehall-settings-开发目标.md`

- [x] Record the confirmed Lifehall settings scope and update the older “本版不做” text that previously excluded store-code fallback and push configuration.
- [x] Run focused tests and `node --check web/app.js`; focused regression result is 14 passed, 4 subtests passed.
- [x] Run the three configured Python suites per `AGENTS.md` (`venv38`, `venv39`, `venv314`) after all source edits have stopped: 3.8 and 3.9 each report 2920 passed; 3.14 reports 2920 passed and 1153 subtests passed.
- [x] Review `git diff --check`, `git status --short --branch`, and the final diff; leave the branch uncommitted and do not build a package.
