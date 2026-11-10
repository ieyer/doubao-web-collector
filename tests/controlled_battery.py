#!/usr/bin/env python3
"""Controlled test battery for the Doubao web collector (B39 section 6.3).

Runs against a live server started with the FAKE driver. All results are
synthetic by construction and must be reported as such.

Usage:
  controlled_battery.py phase-a    # validation, auth, idempotency, concurrency,
                                   # cancel, retry, unknown, pause isolation
  controlled_battery.py phase-b1   # seed restart scenario (completed + running + queued)
  controlled_battery.py phase-b2   # after service restart: verify recovery semantics

Env: COLLECTOR_BASE (default http://localhost:8790), COLLECTOR_TOKEN (required).
State between phases is kept in /tmp/battery_state.json.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = os.environ.get("COLLECTOR_BASE", "http://localhost:8790")
TOKEN = os.environ["COLLECTOR_TOKEN"]
STATE = "/tmp/battery_state.json"
RESULTS = []


def call(method, path, body=None, key=None, auth=True, raw=False):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if auth:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Idempotency-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read()
            return r.status, (payload if raw else json.loads(payload or b"{}"))
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload or b"{}")
        except Exception:
            return e.code, {}


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def wait_status(tid, targets, timeout=60):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        _, t = call("GET", f"/v1/tasks/{tid}")
        last = t.get("status")
        if last in targets:
            return t
        time.sleep(1.5)
    return t


def submit(prompt, key, qid="T", retry=1, ctx=None):
    body = {
        "question_id": qid,
        "prompt": prompt,
        "platform": "doubao",
        "mode": "web_search",
        "retry_limit": retry,
        "context": ctx or {},
    }
    return call("POST", "/v1/tasks", body, key=key)


def phase_a():
    # --- validation & auth ---
    s, _ = call("POST", "/v1/tasks", {"question_id": "V", "prompt": "x", "platform": "other", "mode": "web_search"}, key="v1")
    check("validation: bad platform -> 400", s == 400, f"got {s}")
    s, _ = call("POST", "/v1/tasks", {"question_id": "V", "prompt": "x", "platform": "doubao", "mode": "deep"}, key="v2")
    check("validation: bad mode -> 400", s == 400, f"got {s}")
    s, _ = call("POST", "/v1/tasks", {"question_id": "V", "prompt": "  ", "platform": "doubao", "mode": "web_search"}, key="v3")
    check("validation: empty prompt -> 400", s == 400, f"got {s}")
    s, _ = call("POST", "/v1/tasks", {"question_id": "V", "prompt": "x", "platform": "doubao", "mode": "web_search", "retry_limit": 3}, key="v4")
    check("validation: retry_limit 3 -> 400", s == 400, f"got {s}")
    s, _ = call("GET", "/v1/slots", auth=False)
    check("auth: slots without token -> 401", s == 401, f"got {s}")
    s, _ = call("GET", "/v1/tasks/nonexistent")
    check("unknown task -> 404", s == 404, f"got {s}")

    # --- idempotency ---
    s, t1 = submit("普通问题一", key="idem-1")
    check("submit -> 202", s == 202, f"got {s}")
    s, t1b = submit("普通问题一", key="idem-1")
    check("same key+payload reuses task", t1b.get("task_id") == t1.get("task_id") and t1b.get("reused") is True)
    s, _ = submit("被改动的问题", key="idem-1")
    check("same key+different payload -> 409", s == 409, f"got {s}")
    done = wait_status(t1["task_id"], {"succeeded"}, 60)
    check("idempotent task succeeds once", done["status"] == "succeeded" and len(done["attempts"]) == 1)

    # --- concurrency cap: 7 slow tasks, at most 5 busy slots ---
    tids = []
    for i in range(7):
        _, t = submit(f"__fake_slow__ 并发题 {i}", key=f"conc-{i}")
        tids.append(t["task_id"])
    time.sleep(3)
    _, slots = call("GET", "/v1/slots")
    busy = sum(1 for sl in slots["slots"] if sl["state"] == "busy")
    check("concurrency: busy slots <= 5", busy <= 5, f"busy={busy}")
    check("concurrency: all 5 slots engaged", busy == 5, f"busy={busy}")
    # cancel one queued (7th) while queued
    _, t7 = call("GET", f"/v1/tasks/{tids[6]}")
    if t7["status"] == "queued":
        call("POST", f"/v1/tasks/{tids[6]}/cancel")
        t7b = wait_status(tids[6], {"cancelled"}, 20)
        check("cancel queued -> cancelled, never attempted", t7b["status"] == "cancelled" and len(t7b["attempts"]) == 0)
    else:
        check("cancel queued -> cancelled, never attempted", False, f"7th task was {t7['status']}")
    # cancel one running
    call("POST", f"/v1/tasks/{tids[0]}/cancel")
    t0b = wait_status(tids[0], {"cancelled"}, 30)
    check("cancel running -> cancelled", t0b["status"] == "cancelled", f"got {t0b['status']}")
    for tid in tids[1:6]:
        wait_status(tid, {"succeeded", "failed", "cancelled", "unknown"}, 60)

    # --- retry semantics ---
    _, tr = submit("__fake_presubmit_fail__ 需要重试的题", key="retry-1", retry=1)
    trd = wait_status(tr["task_id"], {"failed"}, 60)
    check("presubmit fail + retry_limit 1 -> 2 attempts then failed",
          trd["status"] == "failed" and len(trd["attempts"]) == 2 and trd["error"]["code"] == "NOT_SUBMITTED",
          f"attempts={len(trd['attempts'])}")
    _, tr0 = submit("__fake_presubmit_fail__ 不重试的题", key="retry-0", retry=0)
    tr0d = wait_status(tr0["task_id"], {"failed"}, 60)
    check("presubmit fail + retry_limit 0 -> 1 attempt", len(tr0d["attempts"]) == 1)

    # --- unknown: submitted but unconfirmable -> no blind resend ---
    _, tu = submit("__fake_unknown__ 结果未知题", key="unk-1", retry=1)
    tud = wait_status(tu["task_id"], {"unknown"}, 60)
    check("submitted-unknown -> status unknown, exactly 1 attempt",
          tud["status"] == "unknown" and len(tud["attempts"]) == 1,
          f"attempts={len(tud['attempts'])}")

    # --- captcha: task fails with code, slot pauses, other slots keep working ---
    _, tc = submit("__fake_captcha__ 验证题", key="cap-1")
    tcd = wait_status(tc["task_id"], {"failed"}, 60)
    check("captcha -> failed VERIFICATION_REQUIRED",
          tcd["status"] == "failed" and tcd["error"]["code"] == "VERIFICATION_REQUIRED")
    _, slots = call("GET", "/v1/slots")
    paused = [sl for sl in slots["slots"] if sl["state"] == "paused"]
    check("captcha -> exactly one slot paused with reason", len(paused) >= 1 and all(sl["reason"] for sl in paused))
    _, tn = submit("验证后普通题", key="after-cap")
    tnd = wait_status(tn["task_id"], {"succeeded"}, 60)
    check("other slots keep working after captcha pause", tnd["status"] == "succeeded")

    # --- login expired pause ---
    _, tl = submit("__fake_login__ 登录失效题", key="login-1")
    tld = wait_status(tl["task_id"], {"failed"}, 60)
    check("login expired -> failed LOGIN_EXPIRED",
          tld["status"] == "failed" and tld["error"]["code"] == "LOGIN_EXPIRED")

    failed = [r for r in RESULTS if not r[1]]
    print(f"\nphase-a: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


def phase_b1():
    _, td = submit("重启前完成题", key="rst-done")
    done = wait_status(td["task_id"], {"succeeded"}, 60)
    assert done["status"] == "succeeded"
    tids = []
    for i in range(5):
        _, t = submit(f"__fake_slow__ 重启占用题 {i}", key=f"rst-slow-{i}")
        tids.append(t["task_id"])
    _, tq = submit("重启时排队题", key="rst-queued")
    time.sleep(3)
    state = {"done": td["task_id"], "running": tids, "queued": tq["task_id"]}
    json.dump(state, open(STATE, "w"))
    print("phase-b1 seeded:", state)
    return 0


def phase_b2():
    state = json.load(open(STATE))
    _, td = call("GET", f"/v1/tasks/{state['done']}")
    check("restart: completed result survives and is readable",
          td["status"] == "succeeded" and bool((td.get("result") or {}).get("answer")))
    unknown_ok = True
    for tid in state["running"]:
        _, t = call("GET", f"/v1/tasks/{tid}")
        if t["status"] == "cancelled":
            continue
        if t["status"] != "unknown" or (t.get("error") or {}).get("code") != "SERVICE_RESTART_UNVERIFIED":
            unknown_ok = False
            print("  running task state:", tid[:8], t["status"], t.get("error"))
    check("restart: in-flight tasks -> unknown (no blind requeue/resend)", unknown_ok)
    tq = wait_status(state["queued"], {"succeeded"}, 90)
    check("restart: never-submitted queued task resumes and completes", tq["status"] == "succeeded", f"got {tq['status']}")
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nphase-b2: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "phase-a"
    sys.exit({"phase-a": phase_a, "phase-b1": phase_b1, "phase-b2": phase_b2}[phase]())
