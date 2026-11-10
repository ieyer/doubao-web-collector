"""Driver layer: one interface, two implementations.

- DoubaoDriver: real browser execution against doubao.com web chat
  (normal web-search mode only; no API, no app, no deep-thinking).
- FakeDriver: deterministic stand-in used by the controlled test battery
  (idempotency / cancel / retry / restart semantics). It never talks to
  Doubao; test reports must label results produced with it as synthetic.

Outcome contract mirrors task book B39 section 4. Fields that cannot be
observed are reported as unknown (None + reason), never fabricated.
"""
import hashlib
import re
import time


class VerificationRequired(Exception):
    """Platform shows a human verification challenge. Never bypassed."""


class LoginExpired(Exception):
    """Slot session is no longer logged in; slot must pause for maintenance."""


class NotSubmitted(Exception):
    """Failure happened before the question was confirmed submitted. Safe to retry."""


class OutcomeUnknown(Exception):
    """Question was submitted but the result cannot be confirmed. Never blindly resent."""


class CancelledByRequest(Exception):
    pass


def empty_result():
    return {
        "answer": None,
        "web_search_observed": None,
        "web_search_signals": [],
        "search_queries": {"items": [], "known": False},
        "references": {"items": [], "complete": None},
        "citations": None,
        "citations_note": None,
        "conversation_url": None,
        "response_kind": None,
        "observed_mode": None,
        "entry_url": "https://www.doubao.com/chat/",
    }


# A 1x1 transparent PNG, used by FakeDriver screenshots.
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000000020001e221bc330000"
    "000049454e44ae426082"
)


class FakeDriver:
    name = "fake"

    def __init__(self, slot_id):
        self.slot_id = slot_id

    def run(self, prompt, cancel_check, on_submitted, timeout_sec=360):
        if "__fake_captcha__" in prompt:
            raise VerificationRequired("平台要求真人验证（合成）")
        if "__fake_login__" in prompt:
            raise LoginExpired("登录态失效（合成）")
        if "__fake_presubmit_fail__" in prompt:
            raise NotSubmitted("提交前失败（合成）")
        if cancel_check():
            raise CancelledByRequest()
        on_submitted()
        if "__fake_unknown__" in prompt:
            raise OutcomeUnknown("已提交但结果无法确认（合成）")
        if "__fake_slow__" in prompt:
            for _ in range(100):
                if cancel_check():
                    raise CancelledByRequest()
                time.sleep(0.2)
        res = empty_result()
        res["answer"] = {
            "text": f"这是对问题「{prompt}」的合成回答。",
            "markdown": f"这是对问题「{prompt}」的合成回答。",
            "completeness": "complete",
        }
        res["web_search_observed"] = True
        res["web_search_signals"] = ["synthetic"]
        res["search_queries"] = {"items": [], "known": False}
        res["references"] = {
            "items": [{"title": "合成来源", "url": "https://example.com/source", "site": "example.com"}],
            "complete": True,
        }
        res["citations"] = []
        res["conversation_url"] = "https://www.doubao.com/chat/fake-conversation"
        res["response_kind"] = "answer"
        res["observed_mode"] = "synthetic"
        shots = [(_PNG_1PX, "synthetic segment 1/1 (question and answer)")]
        return res, shots


# ---------------------------------------------------------------------------
# Real Doubao web driver
# ---------------------------------------------------------------------------

_CAPTCHA_MARKERS = ("拖拽到", "请选择所有符合", "向右滑动", "完成拼图")
_REFUSAL_MARKERS = ("抱歉，我无法", "抱歉，我不能", "无法提供该", "不能提供该信息")


class DoubaoDriver:
    name = "doubao"

    def __init__(self, slot_id, profile_dir):
        self.slot_id = slot_id
        self.profile_dir = profile_dir
        self._pw = None
        self._ctx = None
        self._page = None

    # -- lifecycle ---------------------------------------------------------
    def _ensure_browser(self):
        if self._ctx is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            headless=False,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 1200},
            locale="zh-CN",
        )
        self._ctx.set_default_timeout(15000)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def close(self):
        try:
            if self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._ctx = None
            self._pw = None
            self._page = None

    # -- helpers -----------------------------------------------------------
    def _all_frames_text(self):
        texts = []
        for fr in self._page.frames:
            try:
                texts.append(fr.locator("body").inner_text(timeout=1500))
            except Exception:
                pass
        return texts

    def _captcha_present(self):
        for t in self._all_frames_text():
            if any(m in t for m in _CAPTCHA_MARKERS):
                return True
        for fr in self._page.frames:
            if "verify" in fr.url or "captcha" in fr.url:
                return True
        return False

    def _body_text(self):
        try:
            return self._page.locator("body").inner_text()
        except Exception:
            return ""

    def _check_logged_in(self):
        self._page.goto(
            "https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=60000
        )
        self._page.wait_for_timeout(3500)
        body = self._body_text()
        if "受区域限制" in body or ("/chat" not in self._page.url and "登录" in body):
            raise LoginExpired("页面显示未登录（地区限制页/登录入口），需人工维护登录")

    def _new_conversation(self):
        try:
            nc = self._page.locator("text=新对话").first
            if nc.count() and nc.is_visible():
                nc.click()
                self._page.wait_for_timeout(2000)
        except Exception:
            self._page.goto(
                "https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=60000
            )
            self._page.wait_for_timeout(2500)

    def _observed_mode(self):
        try:
            return self._page.evaluate(
                """() => {
                  const cand = Array.from(document.querySelectorAll('div,button,span'))
                    .filter(e => e.offsetParent && ['快速','深度思考','专家','思考'].includes((e.innerText||'').trim()));
                  return cand.length ? cand[0].innerText.trim() : null;
                }"""
            )
        except Exception:
            return None

    # -- main --------------------------------------------------------------
    def run(self, prompt, cancel_check, on_submitted, timeout_sec=360):
        self._ensure_browser()
        page = self._page
        self._check_logged_in()
        self._new_conversation()
        if self._captcha_present():
            raise VerificationRequired("进入对话前出现真人验证")
        composer = page.locator("[contenteditable='true']").last
        if not composer.count():
            raise NotSubmitted("未找到输入框")
        composer.click()
        page.keyboard.type(prompt, delay=20)
        page.wait_for_timeout(500)
        # verify the composer holds the verbatim prompt before sending
        held = ""
        try:
            held = composer.inner_text()
        except Exception:
            pass
        if prompt not in held:
            raise NotSubmitted("输入框题文与提交题文不一致，未发送")
        page.keyboard.press("Enter")
        page.wait_for_timeout(2500)
        if self._captcha_present():
            raise VerificationRequired("提交后触发真人验证，题目被平台拦截")
        body = self._body_text()
        if prompt not in body:
            raise OutcomeUnknown("已按回车但未在会话中确认题文气泡")
        on_submitted()
        conversation_url = page.url

        # ---- wait for generation to complete ----
        deadline = time.time() + timeout_sec
        last_len = -1
        stable_polls = 0
        signals = []
        while time.time() < deadline:
            if cancel_check():
                self._try_stop()
                raise CancelledByRequest()
            if self._captcha_present():
                raise VerificationRequired("生成过程中出现真人验证")
            state = page.evaluate(
                """() => {
                  const txt = document.body.innerText;
                  const stop = !!Array.from(document.querySelectorAll('button,[role=button],div,span'))
                    .find(e => e.offsetParent && (e.innerText||'').trim() === '停止生成');
                  const boxes = document.querySelectorAll('.md-box-root');
                  let ansLen = 0, actionBar = false;
                  if (boxes.length) {
                    const last = boxes[boxes.length - 1];
                    ansLen = last.innerText.length;
                    let anc = last;
                    for (let i = 0; i < 6 && anc; i++) {
                      anc = anc.parentElement;
                      if (anc && anc.querySelector('[class*=message-action-bar], [class*=action-bar]')) { actionBar = true; break; }
                    }
                  }
                  return {stop, ansLen, actionBar, total: txt.length};
                }"""
            )
            if state["ansLen"] == last_len and state["ansLen"] > 0:
                stable_polls += 1
            else:
                stable_polls = 0
            last_len = state["ansLen"]
            if state["actionBar"] and not state["stop"] and stable_polls >= 2:
                signals.append("action_bar_present")
                signals.append("no_stop_control")
                signals.append("answer_text_stable")
                break
            page.wait_for_timeout(3000)
        else:
            raise OutcomeUnknown("等待回答结束超时且无法确认完成状态")

        result = self._extract(prompt, signals)
        result["conversation_url"] = page.url or conversation_url
        result["observed_mode"] = self._observed_mode()
        shots = self._screenshots()
        return result, shots

    def _try_stop(self):
        try:
            btn = self._page.locator("text=停止生成").first
            if btn.count() and btn.is_visible():
                btn.click()
        except Exception:
            pass
        try:
            self._page.goto(
                "https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30000
            )
        except Exception:
            pass

    # -- extraction ----------------------------------------------------------
    def _search_indicator(self):
        """The web UI shows a line like '搜索 5 个关键词，参考 23 篇资料'
        above a web-searched answer. In the live DOM the line can sit
        inside a collapsible search-process block whose smallest text
        ancestor is long, so this searches the raw page text rather than
        capping element length. Returns the matched line, or None."""
        try:
            return self._page.evaluate(
                """() => {
                  const body = document.body.innerText || '';
                  const m = body.match(/搜索\\s*\\d+\\s*个关键词[^\\n]{0,30}参考\\s*\\d+\\s*篇资料/)
                         || body.match(/参考\\s*\\d+\\s*篇资料/);
                  return m ? m[0].trim() : null;
                }"""
            )
        except Exception:
            return None

    def _open_references_panel(self):
        """Click the search indicator and harvest the reference anchors.

        Before the click the conversation page carries no external
        anchors, so every external anchor afterwards belongs to the
        references panel. Scrolls the panel until the count is stable.
        Returns (items, panel_text_head)."""
        page = self._page
        clicked = False
        try:
            point = page.evaluate(
                """() => {
                  const els = Array.from(document.querySelectorAll('div,span,button,a'))
                    .filter(e => /参考\\s*\\d+\\s*篇资料/.test(e.innerText || ''))
                    .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                  if (!els.length) return null;
                  const el = els[0];
                  el.scrollIntoView({block: 'center'});
                  const r = el.getBoundingClientRect();
                  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }"""
            )
            if point:
                page.mouse.click(point["x"], point["y"])
                clicked = True
        except Exception:
            clicked = False
        if not clicked:
            try:
                loc = page.locator("div,span,button,a").filter(has_text="篇资料").last
                if not loc.count():
                    return [], ""
                loc.click(timeout=8000)
            except Exception:
                return [], ""
        page.wait_for_timeout(2500)
        collect_js = """() => {
          const out = [];
          document.querySelectorAll('a[href]').forEach(a => {
            const h = a.href || '';
            if (h && h.startsWith('http') && !h.includes('doubao.com')) {
              out.push({t: (a.innerText || '').trim(), href: h});
            }
          });
          return out;
        }"""
        scroll_js = """() => {
          const a = Array.from(document.querySelectorAll('a[href]'))
            .filter(x => (x.href||'').startsWith('http') && !x.href.includes('doubao.com'));
          if (!a.length) return;
          // scroll the panel's own scroll container if there is one,
          // and bring the last item into view (virtualized lists recycle
          // nodes, so callers must union across passes)
          let el = a[0];
          while (el && el !== document.body) {
            const s = getComputedStyle(el);
            if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
              el.scrollTop = el.scrollHeight;
              break;
            }
            el = el.parentElement;
          }
          a[a.length - 1].scrollIntoView({block: 'end'});
        }"""
        # union across passes: a virtualized panel replaces earlier items
        # instead of appending, so keeping only the longest single pass
        # silently drops most of the list
        union = {}
        order = []
        stable = 0
        for _ in range(30):
            items = page.evaluate(collect_js)
            grew = False
            for it in items:
                if it["href"] not in union:
                    union[it["href"]] = it
                    order.append(it["href"])
                    grew = True
            if grew:
                stable = 0
            else:
                stable += 1
                if stable >= 3:
                    break
            try:
                page.evaluate(scroll_js)
            except Exception:
                pass
            page.wait_for_timeout(650)
        best = [union[h] for h in order]
        panel_head = ""
        try:
            panel_head = page.evaluate(
                """() => {
                  const a = Array.from(document.querySelectorAll('a[href]'))
                    .find(x => (x.href||'').startsWith('http') && !x.href.includes('doubao.com'));
                  if (!a) return '';
                  let el = a;
                  for (let i = 0; i < 8 && el.parentElement; i++) el = el.parentElement;
                  return (el.innerText || '').slice(0, 1500);
                }"""
            )
        except Exception:
            pass
        refs = []
        seen = set()
        for a in best:
            href = a["href"]
            if href in seen:
                continue
            seen.add(href)
            raw = re.sub(r"^\s*\d+\s*[.、．]\s*", "", a["t"] or "")
            first_line = raw.split("\n")[0].strip()
            title = re.sub(r"\s+", " ", first_line if len(first_line) >= 4 else raw.replace("\n", " ")).strip()[:200]
            m = re.match(r"https?://([^/]+)", href)
            refs.append({"title": title, "url": href, "site": m.group(1) if m else ""})
        # close the panel again so the segmented screenshots show the
        # clean conversation, not the overlay
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
        except Exception:
            pass
        return refs, panel_head or ""

    def _extract(self, prompt, signals):
        page = self._page
        indicator = self._search_indicator()
        if not indicator:
            # the search-process block can settle a moment after the
            # answer's action bar appears; one short retry
            page.wait_for_timeout(2500)
            indicator = self._search_indicator()
        panel_refs, panel_head = ([], "")
        if indicator:
            panel_refs, panel_head = self._open_references_panel()
        data = page.evaluate(
            """() => {
              const boxes = document.querySelectorAll('.md-box-root');
              if (!boxes.length) return null;
              const last = boxes[boxes.length - 1];
              // climb to the message block that also holds references/siblings
              let block = last;
              for (let i = 0; i < 5 && block.parentElement; i++) {
                block = block.parentElement;
                if ((block.innerText || '').length > last.innerText.length * 3) break;
              }
              const anchors = [];
              block.querySelectorAll('a[href]').forEach(a => {
                anchors.push({t: (a.innerText || '').trim().slice(0, 120), href: a.href});
              });
              const bodyLinks = [];
              last.querySelectorAll('a[href]').forEach(a => {
                bodyLinks.push({t: (a.innerText || '').trim().slice(0, 120), href: a.href});
              });
              return {
                answerText: last.innerText,
                answerHTML: last.innerHTML,
                blockText: block.innerText,
                blockAnchors: anchors,
                bodyAnchors: bodyLinks,
                pageText: document.body.innerText.slice(0, 20000)
              };
            }"""
        )
        res = empty_result()
        if not data:
            res["answer"] = {"text": "", "markdown": "", "completeness": "partial"}
            return res
        text = data["answerText"].strip()
        res["answer"] = {
            "text": text,
            "markdown": _html_to_markdown(data["answerHTML"]),
            "completeness": "complete",
        }
        if any(m in text for m in _REFUSAL_MARKERS):
            res["response_kind"] = "refusal"
        else:
            res["response_kind"] = "answer"

        # references: the opened references panel is the primary source;
        # anchors inside the message block (outside the answer body) are a
        # fallback for layouts that render them inline.
        body_hrefs = {a["href"] for a in data["bodyAnchors"]}
        refs = list(panel_refs)
        seen = {r["url"] for r in refs}
        for a in data["blockAnchors"]:
            if a["href"] in body_hrefs or a["href"] in seen:
                continue
            if not a["href"].startswith("http"):
                continue
            seen.add(a["href"])
            site = ""
            m = re.match(r"https?://([^/]+)", a["href"])
            if m:
                site = m.group(1)
            refs.append({"title": a["t"], "url": a["href"], "site": site})
        page_text = data["pageText"]
        has_ref_header = any(
            k in data["blockText"] for k in ("参考资料", "搜索资料", "引用来源", "参考来源")
        )
        declared_count = None
        keyword_count = None
        if indicator:
            m = re.search(r"参考\s*(\d+)\s*篇资料", indicator)
            if m:
                declared_count = int(m.group(1))
            m = re.search(r"搜索\s*(\d+)\s*个关键词", indicator)
            if m:
                keyword_count = int(m.group(1))
        if declared_count is None:
            m = re.search(r"(?:参考资料|搜索资料|引用来源|参考来源)\s*[（(]?(\d+)", data["blockText"])
            if m:
                declared_count = int(m.group(1))
        complete = None
        if declared_count is not None:
            complete = declared_count == len(refs)
        elif has_ref_header or panel_refs:
            complete = None
        res["references"] = {"items": refs, "complete": complete, "declared_count": declared_count}

        # web search observation
        sig = list(signals)
        if indicator:
            sig.append(f"search_indicator: {indicator}")
            res["web_search_observed"] = True
        if has_ref_header:
            sig.append("references_block_present")
        if refs:
            sig.append("reference_links_found")
        if "已搜索" in page_text or "搜索了" in page_text:
            sig.append("search_status_text")
            res["web_search_observed"] = True
        elif refs or has_ref_header:
            res["web_search_observed"] = True
        res["web_search_signals"] = sig

        # search queries: only the words the page literally shows. The
        # indicator declares a keyword count, not the words themselves;
        # the opened panel sometimes lists them -- harvest lines that are
        # not reference titles. Otherwise known=false with the count kept.
        queries = []
        for m in re.finditer(r"(?:搜索关键词|搜索词|已搜索)[:：]\s*([^\n]{2,60})", page_text):
            queries.append(m.group(1).strip())
        res["search_queries"] = {
            "items": queries,
            "known": bool(queries),
            "declared_count": keyword_count,
        }

        # citations: inline [n] markers in the answer text
        marks = [(int(n), idx) for n, idx in
                 [(mm.group(1), mm.start()) for mm in re.finditer(r"\[(\d{1,2})\]", text)]]
        if marks and refs:
            if max(n for n, _ in marks) <= len(refs):
                res["citations"] = [
                    {"marker": f"[{n}]", "position": pos, "reference_index": n - 1,
                     "url": refs[n - 1]["url"], "title": refs[n - 1]["title"]}
                    for n, pos in marks
                ]
            else:
                res["citations"] = None
                res["citations_note"] = "正文引用标记编号超出资料列表长度，无法可靠映射"
        elif marks and not refs:
            res["citations"] = None
            res["citations_note"] = "正文存在引用标记但未取得资料列表，无法映射"
        else:
            res["citations"] = []
            if res["web_search_observed"]:
                res["citations_note"] = "网页端本次回答未显示正文内引用标记（资料经侧栏面板提供）"
        return res

    def _screenshots(self):
        """Segmented screenshots of the conversation column only (no sidebar)."""
        page = self._page
        shots = []
        try:
            geo = page.evaluate(
                """() => {
                  const nav = document.querySelector('[data-testid="chat_route_layout_leftside_nav"]');
                  const navW = nav ? nav.getBoundingClientRect().width : 283;
                  let scroller = null;
                  const boxes = document.querySelectorAll('.md-box-root');
                  if (boxes.length) {
                    let el = boxes[boxes.length - 1];
                    while (el && el !== document.body) {
                      const st = getComputedStyle(el);
                      if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) { scroller = el; break; }
                      el = el.parentElement;
                    }
                  }
                  if (scroller) {
                    scroller.setAttribute('data-collector-scroller', '1');
                    return {navW, scrollHeight: scroller.scrollHeight, clientHeight: scroller.clientHeight, found: true};
                  }
                  return {navW, scrollHeight: document.body.scrollHeight, clientHeight: window.innerHeight, found: false};
                }"""
            )
            nav_w = int(geo["navW"]) + 6
            width = 1366 - nav_w
            total = geo["scrollHeight"]
            step = geo["clientHeight"]
            positions = list(range(0, max(total, 1), max(step - 100, 400)))[:14]
            total_seg = len(positions)
            for i, pos in enumerate(positions):
                if geo["found"]:
                    page.evaluate(
                        "(p) => { document.querySelector('[data-collector-scroller]').scrollTop = p; }",
                        pos,
                    )
                else:
                    page.evaluate("(p) => window.scrollTo(0, p)", pos)
                page.wait_for_timeout(600)
                png = page.screenshot(clip={"x": nav_w, "y": 0, "width": width, "height": min(step, 1200)})
                shots.append((png, f"conversation segment {i+1}/{total_seg}"))
            if geo["found"]:
                page.evaluate(
                    "() => { document.querySelector('[data-collector-scroller]').scrollTop = 0; }"
                )
        except Exception:
            try:
                png = page.screenshot()
                shots.append((png, "viewport fallback (segment capture failed)"))
            except Exception:
                pass
        return shots


def _html_to_markdown(html):
    """Small best-effort HTML -> markdown serializer for answer bodies."""
    if not html:
        return ""
    s = html
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>", lambda m: "\n" + "#" * int(m.group(1)) + " " + m.group(2) + "\n", s, flags=re.S)
    s = re.sub(r"<strong[^>]*>(.*?)</strong>", r"**\1**", s, flags=re.S)
    s = re.sub(r"<b[^>]*>(.*?)</b>", r"**\1**", s, flags=re.S)
    s = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", s, flags=re.S)
    s = re.sub(r"<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"[\2](\1)", s, flags=re.S)
    s = re.sub(r"</p>", "\n\n", s)
    s = re.sub(r"<pre[^>]*>(.*?)</pre>", lambda m: "\n```\n" + re.sub(r"<[^>]+>", "", m.group(1)) + "\n```\n", s, flags=re.S)
    s = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
           .replace("&gt;", ">").replace("&quot;", '"'))
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def shot_sha256(png_bytes):
    return hashlib.sha256(png_bytes).hexdigest()
