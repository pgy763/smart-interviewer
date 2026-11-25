"""端到端 API 流程验证（mock 通道，不需要任何模型 Key）。

跑法： /d/Soft/conda/python.exe tools/e2e_api.py
它做四件事：
  1. 建一场面试（4 题，追问上限 1）
  2. 逐题作答，中间插入带人脸 / 不带人脸的帧
  3. 校验每轮返回的 action / question 是否合理
  4. 拉报告，校验总分、维度、兜底、成本字段都在
"""

import base64
import io
import json
import sys
import time

import httpx
from PIL import Image, ImageDraw

BASE = "http://127.0.0.1:8090"
PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def face_jpeg(look_center=True):
    """合成一张"有人脸"的图：背景偏冷色，中间一块肤色椭圆。

    不追求逼真，只要求肤色连通域够大、位置可调 —— 服务端用的是
    肤色阈值 + 最大连通域，所以这种图对它来说和真脸同类。
    """
    w, h = 640, 480
    img = Image.new("RGB", (w, h), (46, 52, 64))
    d = ImageDraw.Draw(img)
    cx = w // 2 if look_center else int(w * 0.12)
    d.ellipse([cx - 95, 90, cx + 95, 340], fill=(222, 178, 148))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def blank_jpeg():
    img = Image.new("RGB", (640, 480), (40, 44, 52))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


ANSWERS = [
    "我用 Redis 的 INCR 给会话发号，冷启动时用 SETNX 从数据库的 max(seq) 恢复。"
    "这样做的好处是发号不依赖数据库自增，多个会话之间互不影响。"
    "幂等靠 clientMsgId 加 Redis 键加唯一索引三层，重复提交只返回原来的 msgId。",
    "粘包和半包是因为 TCP 是字节流没有消息边界。我的解码器先读 18 字节头，"
    "头不全就直接返回等更多数据；body 没到齐就 resetReaderIndex 退回去。"
    "实测一次发三帧 requestId 不会串号，拆成两半间隔 0.3 秒也能正确重组。",
    "压测是 50 条长连接每连接 5 QPS，实际跑到 256 msg/s，P99 大概 290 毫秒。"
    "瓶颈不在连接数，在 MySQL 事务加 Redis INCR，要再往上得先做批量落库。",
    "如果群成员从 200 涨到 5000，写扩散就会变成瓶颈，那时候应该改成读扩散，"
    "或者只推送在线成员、离线的靠拉取。",
]


def main():
    c = httpx.Client(base_url=BASE, timeout=30)

    print("1. 建面试")
    r = c.post("/api/interview", json={
        "candidate_name": "端到端测试",
        "job_role": "后端开发实习生",
        "resume_text": "做了 IM 项目，自研二进制协议",
        "config": {
            "job_role": "后端开发实习生",
            "questions_target": 4,
            "followup_max": 1,
            "difficulty": "medium",
        },
    })
    j = r.json()
    check("创建返回 ok", j.get("ok") is True, j)
    iid = j["interview_id"]
    check("有 interview id", bool(iid))
    first = j.get("first") or {}
    check("首题已生成", bool(first.get("question")), first)
    check("首题动作是 main", first.get("action") == "main", first.get("action"))

    print("2. 摄像头帧（有脸 → 偏移 → 空画面）")
    # 服务端对帧有节流（默认 1200ms 一帧），连发会被 skipped，
    # 所以这里每帧之间要等过节流窗口——这不是 bug，是防止前端高频抽帧打爆 CPU。
    def send_frame(data_url, label):
        r = c.post(f"/api/interview/{iid}/frame", json={"image": data_url})
        j = r.json()
        check(f"{label} 帧接口 ok", j.get("ok") is True, j)
        return j

    j = send_frame(face_jpeg(True), "居中")
    check("检测到人脸", (j.get("frame") or {}).get("face_found") is True, j.get("frame"))
    check("判定在看镜头", (j.get("frame") or {}).get("looking") is True, j.get("frame"))

    time.sleep(1.4)
    j2 = send_frame(face_jpeg(False), "偏左")
    check("偏移的帧仍检测到人脸", (j2.get("frame") or {}).get("face_found") is True, j2.get("frame"))
    check("偏移的帧判为没看镜头", (j2.get("frame") or {}).get("looking") is False, j2.get("frame"))

    time.sleep(1.4)
    j3 = send_frame(blank_jpeg(), "空画面")
    check("空画面没有脸", (j3.get("frame") or {}).get("face_found") is False, j3.get("frame"))

    print("2b. 节流：连发两帧，第二帧应被 skipped")
    c.post(f"/api/interview/{iid}/frame", json={"image": face_jpeg(True)})
    j4 = c.post(f"/api/interview/{iid}/frame", json={"image": face_jpeg(True)}).json()
    check("连发被节流", j4.get("skipped") is True, j4)

    print("3. 逐题作答（一直答到 closing）")
    actions = []
    for i in range(12):
        ans = ANSWERS[i % len(ANSWERS)]
        r = c.post(f"/api/interview/{iid}/answer",
                   json={"answer": ans, "audio_ms": 12000 + i * 1500})
        j = r.json()
        check(f"第 {i+1} 次提交 ok", j.get("ok") is True, j)
        act = j.get("action")
        actions.append(act)
        print(f"        action={act} 题={(j.get('question') or '')[:36]}")
        if act == "closing":
            break

    check("出现过 closing", "closing" in actions, actions)
    check("出现过追问", "followup" in actions, actions)
    check("出现过压力题", "pressure" in actions, actions)
    check("出现过条件变更题", "variant" in actions, actions)

    print("4. 全场答完后重复提交应判定 duplicate")
    r = c.post(f"/api/interview/{iid}/answer", json={"answer": "重复提交的内容"})
    j = r.json()
    check("重复提交返回 duplicate", j.get("duplicate") is True, j)

    print("5. 参数实时调整")
    r = c.post(f"/api/interview/{iid}/adjust", json={"questions_target": 5})
    j = r.json()
    check("调整参数 ok", j.get("ok") is True, j)
    r = c.get(f"/api/interview/{iid}")
    j = r.json()
    cfg_str = str((j.get("interview") or {}).get("config_json", "")).replace(" ", "")
    check("参数已持久化", '"questions_target":5' in cfg_str, cfg_str[:200])

    print("6. 音频事件")
    for kind, payload in [
        ("transcript", {"text": "我先说结论"}),
        ("pause", {"value": 1800}),
        ("speed", {"value": 4.2}),
    ]:
        body = {"kind": kind}
        body.update(payload)
        r = c.post(f"/api/interview/{iid}/audio", json=body)
        check(f"上报 {kind}", r.json().get("ok") is True, r.text[:120])

    print("7. 结束 + 报告")
    r = c.post(f"/api/interview/{iid}/finish", json={"reason": "脚本跑完"})
    check("结束 ok", r.json().get("ok") is True, r.text[:160])

    r = c.get(f"/api/interview/{iid}/report")
    rep = r.json()
    check("报告 ok", rep.get("ok") is True, r.text[:200])
    d = rep.get("report") or rep
    check("有 overall 字段", "overall" in d, list(d)[:12])
    check("有维度列表", isinstance(d.get("dimensions"), list) and len(d["dimensions"]) == 8,
          len(d.get("dimensions") or []))
    check("有兜底摘要", "fallback_summary" in d)
    check("有成本", isinstance(d.get("cost"), dict) and "total_cny" in d["cost"], d.get("cost"))
    check("有时间线", isinstance(d.get("timeline"), list) and len(d["timeline"]) >= 3,
          len(d.get("timeline") or []))
    check("有限制说明", isinstance(d.get("limitations"), list) and len(d["limitations"]) >= 5)

    scored = [x for x in d["dimensions"] if x["status"] == "scored"]
    insuff = [x for x in d["dimensions"] if x["status"] != "scored"]
    print(f"        已出分 {len(scored)} 个：{[x['name'] for x in scored]}")
    print(f"        证据不足 {len(insuff)} 个：{[x['name'] for x in insuff]}")
    check("至少 3 个维度出分", len(scored) >= 3, len(scored))
    check("有维度带证据", any(x.get("evidence") for x in d["dimensions"]))

    print("8. HTML 页面能渲染")
    for path in ["/", f"/interview/{iid}", f"/report/{iid}"]:
        r = c.get(path)
        check(f"{path} 200", r.status_code == 200, r.status_code)
        check(f"{path} 非空 HTML", "<!DOCTYPE html>" in r.text)

    print("9. 报告页含关键字")
    t = c.get(f"/report/{iid}").text
    for kw in ["能力维度", "风险信号", "成本核算", "这份报告不做什么", "证据不足"]:
        check(f"报告含「{kw}」", kw in t)

    print()
    print(f"PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
