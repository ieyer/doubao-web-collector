import hashlib
import json
import os
import secrets

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __version__
from .config import Config
from .store import Store, payload_hash
from .worker import SlotManager

CONFIG = Config()
STORE = Store(CONFIG.db_path())
SLOTS = SlotManager(CONFIG, STORE)


def _read_version_meta():
    sha = "dev"
    p = os.path.join(os.path.dirname(__file__), "..", "VERSION.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                sha = json.load(f).get("source_sha", "dev")
        except Exception:
            pass
    return sha


SOURCE_SHA = _read_version_meta()

app = FastAPI(title="Doubao Web Collector", version=__version__)


@app.on_event("startup")
def _startup():
    SLOTS.start()


def require_auth(authorization: str = Header(default="")):
    token = CONFIG.token
    if not token:
        raise HTTPException(status_code=503, detail={"error_code": "TOKEN_NOT_CONFIGURED", "message": "服务未配置访问令牌"})
    expected = f"Bearer {token}"
    if not secrets.compare_digest(authorization or "", expected):
        raise HTTPException(status_code=401, detail={"error_code": "UNAUTHORIZED", "message": "缺少或无效的访问令牌"})


def task_view(row, include_result=True):
    attempts = []
    for a in STORE.list_attempts(row["id"]):
        attempts.append(
            {
                "attempt_no": a["attempt_no"],
                "slot_id": a["slot_id"],
                "prepared_at": a["prepared_at"],
                "submitted_at": a["submitted_at"],
                "submission_confirmed": bool(a["submission_confirmed"]),
                "finished_at": a["finished_at"],
                "outcome": a["outcome"],
                "error_code": a["error_code"],
                "error_message": a["error_message"],
                "duration_ms": a["duration_ms"],
                "conversation_url": a["conversation_url"],
                "evidence": json.loads(a["evidence_json"]) if a["evidence_json"] else None,
            }
        )
    view = {
        "task_id": row["id"],
        "question_id": row["question_id"],
        "prompt": row["prompt"],
        "platform": row["platform"],
        "channel": "browser",
        "mode": row["mode"],
        "context": json.loads(row["context_json"] or "{}"),
        "status": row["status"],
        "error": (
            {"code": row["error_code"], "message": row["error_message"]}
            if row["error_code"]
            else None
        ),
        "response_kind": row["response_kind"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "cancel_requested_at": row["cancel_requested_at"],
        "attempts": attempts,
        "automation": {
            "service_version": __version__,
            "source_sha": SOURCE_SHA,
            "driver": CONFIG.driver,
        },
    }
    if include_result and row["result_json"]:
        view["result"] = json.loads(row["result_json"])
    return view


@app.get("/v1/health")
def health():
    return {"ok": True, "service": "doubao-web-collector", "version": __version__}


@app.get("/v1/slots")
def slots(authorization: str = Header(default="")):
    require_auth(authorization)
    return {"slots": SLOTS.snapshot(), "max_active": len([s for s in CONFIG.slots if s.enabled])}


@app.post("/v1/tasks")
async def create_task(request: Request, authorization: str = Header(default=""), idempotency_key: str = Header(default="")):
    require_auth(authorization)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error_code": "BAD_JSON", "message": "请求体不是合法 JSON"})
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail={"error_code": "EMPTY_PROMPT", "message": "prompt 为必填非空题文"})
    if len(prompt) > 2000:
        raise HTTPException(status_code=400, detail={"error_code": "PROMPT_TOO_LONG", "message": "题文超过 2000 字上限（不静默截断）"})
    if body.get("platform") != "doubao":
        raise HTTPException(status_code=400, detail={"error_code": "UNSUPPORTED_PLATFORM", "message": "platform 只接受 doubao"})
    if body.get("mode") != "web_search":
        raise HTTPException(status_code=400, detail={"error_code": "UNSUPPORTED_MODE", "message": "mode 只接受 web_search"})
    retry_limit = body.get("retry_limit", 1)
    if retry_limit not in (0, 1):
        raise HTTPException(status_code=400, detail={"error_code": "BAD_RETRY_LIMIT", "message": "retry_limit 只允许 0 或 1"})
    if STORE.count_queued() >= CONFIG.queue_limit:
        raise HTTPException(status_code=429, detail={"error_code": "QUEUE_FULL", "message": "队列已满"})
    key = idempotency_key or body.get("idempotency_key") or hashlib.sha256(
        f"{body.get('question_id')}|{prompt}|{json.dumps(body.get('context') or {}, sort_keys=True)}".encode("utf-8")
    ).hexdigest()
    row, how = STORE.create_or_reuse(key, payload_hash(body), body)
    if how == "conflict":
        raise HTTPException(
            status_code=409,
            detail={"error_code": "IDEMPOTENCY_CONFLICT", "message": "同一 Idempotency-Key 已用于不同业务参数"},
        )
    view = task_view(row, include_result=False)
    view["reused"] = how == "reused"
    return JSONResponse(status_code=202, content=view)


@app.get("/v1/tasks/{task_id}")
def get_task(task_id: str, authorization: str = Header(default="")):
    require_auth(authorization)
    row = STORE.get_task(task_id)
    if not row:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND", "message": "任务不存在"})
    return task_view(row)


@app.post("/v1/tasks/{task_id}/cancel")
def cancel_task(task_id: str, authorization: str = Header(default="")):
    require_auth(authorization)
    row = STORE.request_cancel(task_id)
    if not row:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND", "message": "任务不存在"})
    return task_view(row, include_result=False)


@app.get("/v1/tasks/{task_id}/assets/{asset_id}")
def get_asset(task_id: str, asset_id: str, authorization: str = Header(default="")):
    require_auth(authorization)
    asset = STORE.get_asset(task_id, asset_id)
    if not asset or not os.path.exists(asset["path"]):
        raise HTTPException(status_code=404, detail={"error_code": "ASSET_NOT_FOUND", "message": "截图不存在"})
    return FileResponse(asset["path"], media_type="image/png")


@app.get("/", response_class=HTMLResponse)
def debug_page():
    return DEBUG_HTML


DEBUG_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>豆包网页采集器 · 调试页</title>
<style>
body{font-family:system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#1a1a1a}
input,textarea,button{font-size:14px;padding:8px;margin:4px 0}
textarea{width:100%;height:72px}
button{background:#111;color:#fff;border:0;border-radius:6px;padding:8px 16px;cursor:pointer}
button.secondary{background:#666}
pre{background:#f5f5f5;padding:12px;overflow:auto;white-space:pre-wrap;word-break:break-all}
img.shot{max-width:100%;border:1px solid #ddd;margin:6px 0}
.tag{display:inline-block;background:#e8e8e8;border-radius:4px;padding:1px 7px;margin-right:6px;font-size:12px}
</style></head><body>
<h2>豆包网页采集器 · 调试页</h2>
<p>访问令牌：<input id="token" type="password" size="40" placeholder="Bearer token">
<button class="secondary" onclick="saveToken()">保存</button>
<button class="secondary" onclick="loadSlots()">刷新槽位</button></p>
<div id="slots"></div>
<hr>
<p>题文（原样提交，不加任何引导）：</p>
<textarea id="prompt">小户型装风管机，有哪些品牌值得考虑？</textarea><br>
question_id <input id="qid" value="Q01" size="10">
retry_limit <input id="rl" value="1" size="3">
<button onclick="submitTask()">提交任务</button>
<div id="taskbox"></div>
<script>
let curTask=null, timer=null;
function tok(){return document.getElementById('token').value.trim()}
function saveToken(){sessionStorage.setItem('ct', tok()); alert('已保存到本页会话')}
document.getElementById('token').value = sessionStorage.getItem('ct')||'';
async function api(path, opts={}){opts.headers=Object.assign({'Authorization':'Bearer '+tok(),'Content-Type':'application/json'},opts.headers||{});const r=await fetch(path,opts);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error((j.detail&&j.detail.message)||r.status);return j}
async function loadSlots(){try{const s=await api('/v1/slots');document.getElementById('slots').innerHTML=s.slots.map(x=>`<span class="tag">${x.slot_id}: ${x.state}${x.reason?'（'+x.reason+'）':''}</span>`).join('')}catch(e){document.getElementById('slots').textContent='槽位读取失败: '+e.message}}
async function submitTask(){
  const body={question_id:document.getElementById('qid').value,prompt:document.getElementById('prompt').value,platform:'doubao',mode:'web_search',retry_limit:parseInt(document.getElementById('rl').value||'1'),context:{run_id:'debug'}};
  try{const t=await api('/v1/tasks',{method:'POST',body:JSON.stringify(body),headers:{'Idempotency-Key':'debug-'+Date.now()}});curTask=t.task_id;poll();}catch(e){alert('提交失败: '+e.message)}}
async function poll(){
  if(timer)clearInterval(timer);
  const show=async()=>{if(!curTask)return;try{const t=await api('/v1/tasks/'+curTask);render(t);if(['succeeded','partial','failed','cancelled','unknown'].includes(t.status))clearInterval(timer);}catch(e){}};
  show(); timer=setInterval(show,3000);}
function render(t){
  let html=`<h3>任务 ${t.task_id.slice(0,8)}… 状态：${t.status}${t.error?' · '+t.error.code+': '+t.error.message:''}</h3>`;
  html+=`<button class="secondary" onclick="cancelTask()">取消任务</button>`;
  if(t.result){
    if(t.result.answer)html+=`<h4>回答（${t.result.answer.completeness}）</h4><pre>${(t.result.answer.text||'').replace(/</g,'&lt;')}</pre>`;
    html+=`<h4>资料与引用</h4><pre>联网观察: ${t.result.web_search_observed}\n搜索词: ${JSON.stringify(t.result.search_queries)}\n资料: ${JSON.stringify(t.result.references,null,1)}\n引用: ${JSON.stringify(t.result.citations)}\n会话: ${t.result.conversation_url||''}</pre>`;
    (t.result.screenshots||[]).forEach(s=>{html+=`<div>${s.coverage}</div><img class="shot" src="/v1/tasks/${t.task_id}/assets/${s.asset_id}" onload="this.previousElementSibling=null" onerror="this.outerHTML='<p>截图需带令牌读取，请用接口查看</p>'">`});
  }
  html+=`<h4>原始记录</h4><pre>${JSON.stringify(t,null,1).replace(/</g,'&lt;')}</pre>`;
  document.getElementById('taskbox').innerHTML=html;
  loadSlots();}
async function cancelTask(){if(!curTask)return;await api('/v1/tasks/'+curTask+'/cancel',{method:'POST'});poll();}
loadSlots();
</script></body></html>
"""
