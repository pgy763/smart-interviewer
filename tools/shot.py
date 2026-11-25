"""用 headless Edge + CDP 给三个页面截图，并顺手做几条 DOM 断言。

为什么不用 playwright：本机没装，且沙箱里 pip install 拿不到（权限被拒）。
Edge 自带 DevTools 协议，websocket-client 已装，够用。

运行：
    /d/Soft/conda/python.exe tools/shot.py
需要 danger-full-access（要 spawn Edge 子进程）。

注意：摄像头用 `--use-fake-device-for-media-stream` 假装有一个，
否则 getUserMedia 在无摄像头环境会 reject，面试页的观察区是空的。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BASE = "http://127.0.0.1:8090"
PORT = 9333
PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_edge_profile")
# 合成"人脸"视频，给假摄像头当输入。
# 不用 `--use-fake-device-for-media-stream` 默认那个绿色测试图案——它没有肤色区域，
# 视觉启发式会（正确地）判"未检测到人脸"，演示图里就成了「离开画面 Ns」，
# 看起来像链路坏了。
FAKE_VIDEO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_face.y4m")
SHOTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_shots")


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.n = 0

    def call(self, method: str, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr,
                      returnByValue=True, awaitPromise=True)
        res = r.get("result", {})
        if r.get("exceptionDetails"):
            raise RuntimeError(f"JS 异常: {r['exceptionDetails'].get('text')} :: {expr[:120]}")
        return res.get("value")

    def goto(self, url: str, wait: float = 2.5):
        self.call("Page.navigate", url=url)
        time.sleep(wait)

    def shot(self, name: str, full: bool = False):
        r = self.call("Page.captureScreenshot", format="png",
                      captureBeyondViewport=full)
        import base64
        path = os.path.join(SHOTS, name)
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        print(f"   截图 {name} ({os.path.getsize(path)} 字节)")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def wait_devtools(timeout=30.0) -> str:
    """等 DevTools 起来，返回**页面 target** 的 ws 地址。

    注意 `/json/version` 给的是 browser 级 ws 地址，在它上面发 `Page.enable`
    会报 `'Page.enable' wasn't found`（browser target 只有 Target/CDP 等域，
    没有 Page/Runtime）。要连具体页面的 target，得从 `/json/list` 里挑 type=page。
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2) as r:
                targets = json.loads(r.read().decode())
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("Edge DevTools 没起来（或没有 page target）")


def main():
    os.makedirs(SHOTS, exist_ok=True)
    shutil.rmtree(PROFILE, ignore_errors=True)
    os.makedirs(PROFILE, exist_ok=True)

    proc = subprocess.Popen([
        EDGE,
        "--headless=new",
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROFILE}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", "--hide-scrollbars",
        # 假装有摄像头/麦克风，否则 getUserMedia 直接 reject
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        f"--use-file-for-fake-video-capture={FAKE_VIDEO}",
        "--window-size=1440,980",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        ws_url = wait_devtools()
        cdp = CDP(ws_url)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 width=1440, height=980, deviceScaleFactor=1, mobile=False)

        # ---------------- 1. 控制台 ----------------
        print("1. 控制台首页")
        cdp.goto(f"{BASE}/", 2.0)
        title = cdp.eval("document.title")
        print("   title =", title)
        txt = cdp.eval("document.body.innerText")
        assert "新建" in txt and "面试" in txt, "首页没找到新建表单"
        assert "能力维度" in txt, "首页没找到维度面板"
        cdp.shot("s1-console.png")

        # ---------------- 2. 建面试 + 面试页 ----------------
        print("2. 建一场面试（直接打 API，绕开表单）")
        created = cdp.eval("""
            (async () => {
              const r = await fetch('/api/interview', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                  candidate_name: '文博玉',
                  job_role: '后端开发实习生',
                  resume_text: '鱼见 IM：Netty 自研二进制协议、Redis INCR 发号、159 项断言',
                  config: {job_role: '后端开发实习生', questions_target: 4, followup_max: 1,
                           base_minutes: 20, max_minutes: 45, pressure_round: true, variant_round: true}
                })
              });
              return JSON.stringify(await r.json());
            })()
        """)
        data = json.loads(created)
        iid = data["interview_id"]
        print("   interview_id =", iid)
        assert data["first"]["question"], "第一题是空的"

        print("3. 面试页")
        cdp.goto(f"{BASE}/interview/{iid}", 3.5)
        txt = cdp.eval("document.body.innerText")
        assert "文博玉" in txt, "面试页没显示候选人"
        assert "摄像头" in txt or "video" in txt.lower(), "面试页没有摄像头区"
        # 点「开启摄像头」，让观察区真的出画面（Edge 用 --use-fake-device 假装有摄像头）
        clicked = cdp.eval("""(() => {
            const b = [...document.querySelectorAll('button')].find(x => /开启摄像头/.test(x.innerText));
            if (b) { b.click(); return true; } return false;
        })()""")
        print("   点开启摄像头 =", clicked)
        time.sleep(3.0)
        cam = cdp.eval("(() => { const v = document.querySelector('video'); "
                       "return v ? {w: v.videoWidth, h: v.videoHeight, rs: v.readyState} : null; })()")
        print("   video 元素状态 =", cam)
        # 再等一轮抽帧（前端每 3 秒抓一帧），让报告里真的有视觉数据
        time.sleep(6.5)
        frames = cdp.eval("(async () => { const r = await fetch('/api/interview/%s'); "
                          "const j = await r.json(); return j.steps ? 0 : 1; })()" % iid)
        cdp.shot("s2-interview.png")

        # ---------------- 4. 走完一场（打 API 灌回答） ----------------
        print("4. 把这场面试答完（mock 通道，触发追问/压力/变更）")
        answers = [
            "我用 Netty 自己写了 18 字节的二进制帧协议，头里放魔数、命令字和 bodyLen。"
            "解码器遇到半包就 resetReaderIndex 退回等缓冲，实测粘包三帧一次发出去 requestId 也不会串号。",
            "发号用 Redis INCR，冷启动用 SETNX 从库里的 max(seq) 恢复，P50 是 5 毫秒左右。"
            "幂等靠 clientMsgId 加唯一索引三层去重，重发只回原 msgId。",
            "压测是 50 条长连接每条约 5 QPS，跑出 255 msg/s，成功率 100%，P99 大概 300 毫秒。",
            "如果量级放大一百倍，我会把写扩散改成读扩散，并且把落库改成批量提交。",
            "群聊我做了软删，退群是 left_at 打时间戳，所有查成员的 SQL 都必须带 left_at IS NULL。",
        ]
        for i, a in enumerate(answers):
            r = cdp.eval(
                "(async () => { const r = await fetch('/api/interview/%s/answer', "
                "{method:'POST', headers:{'Content-Type':'application/json'}, "
                "body: JSON.stringify({answer: %s})}); return JSON.stringify(await r.json()); })()"
                % (iid, json.dumps(a))
            )
            d = json.loads(r)
            print(f"   第 {i+1} 轮 → {d.get('action')}: {(d.get('question') or '')[:34]}")
            if d.get("action") == "closing":
                break
        # 兜底：如果还没收尾，显式 finish
        cdp.eval("(async () => { await fetch('/api/interview/%s/finish', {method:'POST'}); "
                 "return 1; })()" % iid)

        # 回面试页看已答内容
        cdp.goto(f"{BASE}/interview/{iid}", 3.0)
        cdp.shot("s3-interview-answered.png")

        # ---------------- 5. 报告页 ----------------
        print("5. 报告页")
        cdp.goto(f"{BASE}/report/{iid}", 2.5)
        txt = cdp.eval("document.body.innerText")
        for kw in ["能力维度", "风险信号", "成本核算", "这份报告不做什么"]:
            assert kw in txt, f"报告页缺「{kw}」"
        score = cdp.eval("(() => { const e = document.querySelector('.score-big'); return e ? e.innerText : ''; })()")
        print("   总分 =", score)
        cdp.shot("s4-report.png")
        cdp.shot("s5-report-full.png", full=True)

        # ---------------- 6. 深色模式 ----------------
        print("6. 深色模式")
        cdp.call("Emulation.setEmulatedMedia",
                 features=[{"name": "prefers-color-scheme", "value": "dark"}])
        cdp.goto(f"{BASE}/report/{iid}", 2.0)
        bg = cdp.eval("getComputedStyle(document.body).backgroundColor")
        print("   body 背景 =", bg)
        nums = [int(x) for x in __import__("re").findall(r"\d+", bg)[:3]]
        assert max(nums) < 80, f"深色模式下背景还是亮的：{bg}"
        cdp.shot("s6-report-dark.png")

        cdp.goto(f"{BASE}/", 2.0)
        cdp.shot("s7-console-dark.png")

        errs = cdp.eval("JSON.stringify(window.__errs || [])")
        print("   window.__errs =", errs)
        cdp.close()
        print("\n全部步骤通过。")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
