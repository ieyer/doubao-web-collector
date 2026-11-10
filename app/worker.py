import os
import threading
import time

from . import drivers
from .store import now_iso

RETRY_BACKOFF_SEC = 5


class Slot:
    def __init__(self, cfg, require_profile=True):
        self.slot_id = cfg.slot_id
        self.profile_dir = cfg.profile_dir
        self.enabled = cfg.enabled
        self.state = "idle" if cfg.enabled else "disabled"
        self.reason = None
        self.current_task_id = None
        if (
            require_profile
            and cfg.enabled
            and not os.path.isdir(os.path.join(cfg.profile_dir, "Default"))
        ):
            self.state = "unconfigured"
            self.reason = "浏览器资料目录未初始化（无账号登录档案）"


class SlotManager:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.slots = [Slot(s, require_profile=(config.driver == "doubao")) for s in config.slots]
        self._threads = []
        self._stop = threading.Event()

    def start(self):
        for slot in self.slots:
            if slot.state in ("idle",):
                t = threading.Thread(target=self._slot_loop, args=(slot,), daemon=True)
                t.start()
                self._threads.append(t)

    def stop(self):
        self._stop.set()

    def snapshot(self):
        out = []
        for s in self.slots:
            out.append(
                {
                    "slot_id": s.slot_id,
                    "state": s.state,
                    "reason": s.reason,
                    "current_task_id": s.current_task_id,
                    "account_configured": os.path.isdir(os.path.join(s.profile_dir, "Default")),
                }
            )
        return out

    def _make_driver(self, slot):
        if self.config.driver == "fake":
            return drivers.FakeDriver(slot.slot_id)
        return drivers.DoubaoDriver(slot.slot_id, slot.profile_dir)

    def _slot_loop(self, slot):
        driver = None
        while not self._stop.is_set():
            task = None
            if slot.state == "idle":
                task = self.store.claim_next(slot.slot_id)
            if not task:
                time.sleep(1.0)
                continue
            slot.state = "busy"
            slot.current_task_id = task["id"]
            try:
                if driver is None:
                    driver = self._make_driver(slot)
                self._run_task(slot, driver, task)
            except Exception as e:  # never let one task kill the slot loop
                self.store.update_task(
                    task["id"],
                    status="failed",
                    error_code="INTERNAL",
                    error_message=f"执行器内部错误: {type(e).__name__}: {str(e)[:200]}",
                    finished_at=now_iso(),
                )
            finally:
                slot.current_task_id = None
                if slot.state == "busy":
                    slot.state = "idle"
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _cancel_requested(self, task_id):
        t = self.store.get_task(task_id)
        return bool(t and t["cancel_requested_at"])

    def _run_task(self, slot, driver, task):
        store = self.store
        tid = task["id"]
        max_attempts = 1 + int(task["retry_limit"])
        attempt_no = 0
        while attempt_no < max_attempts:
            attempt_no += 1
            if self._cancel_requested(tid):
                store.update_task(
                    tid, status="cancelled", finished_at=now_iso(),
                )
                return
            aid = store.start_attempt(tid, attempt_no, slot.slot_id)
            submitted = {"flag": False, "at": None}

            def on_submitted():
                submitted["flag"] = True
                submitted["at"] = now_iso()
                store.finish_attempt(
                    aid, submitted_at=submitted["at"], submission_confirmed=1
                )

            started = time.time()
            try:
                result, shots = driver.run(
                    task["prompt"],
                    cancel_check=lambda: self._cancel_requested(tid),
                    on_submitted=on_submitted,
                    timeout_sec=self.config.run_timeout_sec,
                )
            except drivers.CancelledByRequest:
                store.finish_attempt(
                    aid, finished_at=now_iso(), outcome="cancelled",
                    duration_ms=int((time.time() - started) * 1000),
                )
                store.update_task(tid, status="cancelled", finished_at=now_iso())
                return
            except drivers.VerificationRequired as e:
                store.finish_attempt(
                    aid, finished_at=now_iso(), outcome="failed",
                    error_code="VERIFICATION_REQUIRED", error_message=str(e),
                    duration_ms=int((time.time() - started) * 1000),
                )
                slot.state = "paused"
                slot.reason = f"平台真人验证待人工处理（{now_iso()}）"
                store.update_task(
                    tid, status="failed", error_code="VERIFICATION_REQUIRED",
                    error_message=str(e), finished_at=now_iso(),
                )
                return
            except drivers.LoginExpired as e:
                store.finish_attempt(
                    aid, finished_at=now_iso(), outcome="failed",
                    error_code="LOGIN_EXPIRED", error_message=str(e),
                    duration_ms=int((time.time() - started) * 1000),
                )
                slot.state = "paused"
                slot.reason = f"登录态失效，待人工维护（{now_iso()}）"
                store.update_task(
                    tid, status="failed", error_code="LOGIN_EXPIRED",
                    error_message=str(e), finished_at=now_iso(),
                )
                return
            except drivers.NotSubmitted as e:
                store.finish_attempt(
                    aid, finished_at=now_iso(), outcome="failed",
                    error_code="NOT_SUBMITTED", error_message=str(e),
                    duration_ms=int((time.time() - started) * 1000),
                )
                if attempt_no < max_attempts:
                    time.sleep(RETRY_BACKOFF_SEC)
                    continue
                store.update_task(
                    tid, status="failed", error_code="NOT_SUBMITTED",
                    error_message=str(e), finished_at=now_iso(),
                )
                return
            except drivers.OutcomeUnknown as e:
                store.finish_attempt(
                    aid, finished_at=now_iso(), outcome="unknown",
                    error_code="OUTCOME_UNKNOWN", error_message=str(e),
                    duration_ms=int((time.time() - started) * 1000),
                )
                store.update_task(
                    tid, status="unknown", error_code="OUTCOME_UNKNOWN",
                    error_message=str(e), finished_at=now_iso(),
                )
                return
            except Exception as e:
                store.finish_attempt(
                    aid, finished_at=now_iso(), outcome="failed",
                    error_code="DRIVER_ERROR",
                    error_message=f"{type(e).__name__}: {str(e)[:300]}",
                    duration_ms=int((time.time() - started) * 1000),
                )
                if not submitted["flag"] and attempt_no < max_attempts:
                    time.sleep(RETRY_BACKOFF_SEC)
                    continue
                status = "unknown" if submitted["flag"] else "failed"
                store.update_task(
                    tid, status=status, error_code="DRIVER_ERROR",
                    error_message=f"{type(e).__name__}: {str(e)[:300]}",
                    finished_at=now_iso(),
                )
                return

            # ---- success path: persist evidence, then judge completeness ----
            evidence = {
                "web_search_signals": result.get("web_search_signals", []),
                "observed_mode": result.get("observed_mode"),
                "entry_url": result.get("entry_url"),
                "driver": getattr(driver, "name", "unknown"),
            }
            store.finish_attempt(
                aid, finished_at=now_iso(), outcome="succeeded",
                duration_ms=int((time.time() - started) * 1000),
                conversation_url=result.get("conversation_url"),
                evidence_json=_json(evidence),
            )
            asset_ids = []
            assets_dir = self.config.assets_dir()
            os.makedirs(os.path.join(assets_dir, tid), exist_ok=True)
            for i, (png, coverage) in enumerate(shots):
                sha = drivers.shot_sha256(png)
                path = os.path.join(assets_dir, tid, f"shot-{i+1}.png")
                with open(path, "wb") as f:
                    f.write(png)
                asset_ids.append(
                    store.add_asset(tid, aid, "screenshot", i + 1, coverage, sha, path)
                )
            completeness = (result.get("answer") or {}).get("completeness")
            has_answer = bool((result.get("answer") or {}).get("text"))
            if has_answer and completeness == "complete" and asset_ids:
                status = "succeeded"
            else:
                status = "partial"
            result_out = {
                "answer": result.get("answer"),
                "web_search_observed": result.get("web_search_observed"),
                "search_queries": result.get("search_queries"),
                "references": result.get("references"),
                "citations": result.get("citations"),
                "citations_note": result.get("citations_note"),
                "conversation_url": result.get("conversation_url"),
                "screenshots": [
                    {"asset_id": aid_, "coverage": shots[i][1]}
                    for i, aid_ in enumerate(asset_ids)
                ],
            }
            store.update_task(
                tid,
                status=status,
                response_kind=result.get("response_kind"),
                result_json=_json(result_out),
                finished_at=now_iso(),
            )
            return


def _json(obj):
    import json

    return json.dumps(obj, ensure_ascii=False)
