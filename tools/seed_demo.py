"""播种 2 场体面样例：一场完整 mock（带真实感的回答），一场只有主问题。

删掉 data/interview.db 后跑，服务不需要启动。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.api import session as sess  # noqa: E402

db.init_db()

ANS = [
    "我在鱼见 IM 里用 Spring Boot 加 Netty 自研了 18 字节定长头的二进制协议，"
    "里面放魔数、版本、命令字、requestId 和 body 长度。粘包半包在解码器处理："
    "头不全直接返回等下一次数据，body 不全就 resetReaderIndex 退回。"
    "单连接实测 P50 是 5.01 毫秒，50 条连接 5 QPS 下 P50 81.93 毫秒。",
    "消息不丢不重我做了三层。客户端生成 clientMsgId，服务端查 Redis 幂等键（TTL 两小时），"
    "数据库还有唯一索引兜底。发号用 Redis INCR，冷启动时用 SETNX 从库里最大值恢复。"
    "重发只返回原来的 msgId，而且不会再给对方推一条——这条是最容易漏的。",
    "最难的是雪花 ID 精度丢失。前端拿到 383197437298343936 变成 383197437298343940，"
    "因为 JS 的 Number 最大安全整数只有 9 乘以 10 的 15 次方。"
    "我一开始以为是前端 bug，排查了两小时才定位到 JSON.parse 的舍入。"
    "后来后端全局把 Long 序列化成字符串，seq 这种计数器保持数字不动。",
    "如果重做，我会先把批量落库做出来。现在 50 连接 5 QPS 就到 P50 82 毫秒，"
    "查日志发现时间花在 MySQL 事务提交和 Redis INCR 上，连接数根本不是瓶颈。"
    "批量落库加一次事务提交一百条，能把吞吐提一个数量级。",
    "还有个教训是测试盲区。幂等用例我只断言了 msgId 相同、duplicate 为真、seq 没多消耗，"
    "没断言对方会不会再收到推送。结果端到端脚本一跑就报 FAIL，PASS 27 FAIL 1。"
    "所以幂等不是不要写两条数据，是整条副作用链都得幂等。",
]


def _backdate(iid: str, duration_sec: float) -> None:
    """把播种的场次改成像真的面试过一样。

    播种是脚本跑的，`started_at` 到 `ended_at` 只差几百毫秒。但
    `detect_fallbacks` 会因此判成 `no_progress`（"时长 0"），报告不出分——
    这恰恰是兜底逻辑在正确工作，只是样例看起来像坏数据。所以把时间轴
    往后铺开，让样例是一份正常报告。
    """
    with db.get_conn() as conn:
        row = conn.execute("SELECT ended_at FROM interview WHERE id=?", (iid,)).fetchone()
        end = row["ended_at"] or time.time()
        start = end - duration_sec
        conn.execute("UPDATE interview SET started_at=?, ended_at=? WHERE id=?",
                     (start, end, iid))
        turns = conn.execute(
            "SELECT idx FROM turn WHERE interview_id=? ORDER BY idx", (iid,)).fetchall()
        step = duration_sec / max(len(turns), 1)
        for i, t in enumerate(turns):
            asked = start + step * i
            conn.execute("UPDATE turn SET asked_at=?, answered_at=? WHERE interview_id=? AND idx=?",
                         (asked, asked + step * 0.6, iid, t["idx"]))
        # 视频帧、音频事件也铺到同一区间
        conn.execute("UPDATE vision_frame SET ts = ? + (rowid % 6) * 3.0 WHERE interview_id=?",
                     (start, iid))
        conn.execute("UPDATE audio_event SET ts = ? + (rowid % 10) * 11.0 WHERE interview_id=?",
                     (start, iid))


def seed_full():
    r = sess.manager.create(
        "文博玉", "后端开发实习生",
        "中北大学软件工程 2028 届。鱼见 IM：Spring Boot + Netty 自研二进制协议、"
        "Redis INCR 发号、159 项断言；电商问数系统：LangGraph 多节点 NL2SQL。",
        {"questions_target": 4, "followup_max": 1, "base_minutes": 20, "max_minutes": 45},
    )
    iid = r["interview_id"]
    s = sess.manager.get(iid)
    for a in ANS:
        try:
            res = s.submit(a, 9000)
        except Exception as e:  # noqa: BLE001
            print("submit failed:", e)
            break
        if res.get("action") == "closing":
            break
    # 模拟一段有摄像头的观察记录（6 帧，全部检测到人脸、都看镜头）
    now = time.time()
    for i in range(6):
        db.add_vision_frame(iid, ts=now - (6 - i) * 3.0, face_found=1,
                            face_ratio=0.17, center_dx=0.03, center_dy=-0.02,
                            brightness=118.0, looking=1, motion=0.4,
                            note="肤色块占比 17%，位于画面中央")
    # 语音事件：4 段转写 + 3 次停顿 + 4 个语速点
    for a in ANS[:4]:
        db.add_audio_event(iid, kind="transcript", text=a, meta={"chars": len(a)})
    for ms in (820, 1150, 640):
        db.add_audio_event(iid, kind="pause", value=float(ms))
    for cps in (4.2, 4.6, 3.8, 4.4):
        db.add_audio_event(iid, kind="speed", value=float(cps))
    sess.manager.drop(iid)
    db.set_interview_status(iid, "finished", ended=True)
    _backdate(iid, 13 * 60 + 20)
    print("full:", iid)
    return iid


def seed_thin():
    r = sess.manager.create("李四", "Java 后端开发", "一段简单的自我介绍。",
                            {"questions_target": 2, "followup_max": 0})
    iid = r["interview_id"]
    s = sess.manager.get(iid)
    s.submit("我做过一个博客系统，用 Spring Boot 和 MySQL，支持文章发布和评论。", 5000)
    sess.manager.drop(iid)
    db.set_interview_status(iid, "finished", ended=True)
    _backdate(iid, 4 * 60 + 10)
    print("thin:", iid)
    return iid


if __name__ == "__main__":
    seed_full()
    seed_thin()
