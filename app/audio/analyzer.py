"""音频链路：转写、停顿、语速、填充词。

**架构选择（面试重点）**

真实实时语音有两种做法：

A. 浏览器录音 → 分片上传 WebSocket → 服务端调 Qwen-Omni 双向流 → 回传文字
B. 浏览器直接连 Qwen-Omni（前端拿到临时 token）→ 文字同时发一份给后端

本项目默认走 **A 的半成品：浏览器用 Web Speech API 做本地转写，把文字+时间戳
发给后端**，同时预留 B 的完整实现（`OmniSession`）。

为什么默认不直接上 Omni：需要 API Key，而 demo 必须"clone 下来就能跑"。
但代码里 `OmniSession` 是完整可用的（见下），填 Key 后 `SI_VOICE=omni` 就能切换。
这样面试官问"你们怎么接的实时语音"时，你可以打开文件给他看真实的
WebSocket 双向流实现，而不是说"我们计划接"。

**停顿与语速怎么来的（不靠模型）**
浏览器端在 Web Speech API 的 `onresult` 里能拿到 interim/final 与时间戳，
把"两个 final 片段之间的间隔"记为停顿。"字符数/时长"记为语速。
这些都是**客户端测量、服务端只做聚合**，避免把音频传上去再算。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass

from app import db
from app.config import settings


# ---------------------------------------------------------------------------
# 文本侧特征（服务端算，输入是转写文本 + 停顿序列）
# ---------------------------------------------------------------------------
FILLERS = ["嗯", "啊", "那个", "然后就是", "这个这个", "呃", "就是就是"]
FORMAL_MARKERS = ["综上所述", "因此", "基于", "针对", "在该场景下", "由此可见",
                  "一方面", "另一方面", "显著", "具备", "予以", "进行优化"]


def text_features(text: str) -> dict:
    """从一段回答里抽口语/书面语特征。"""
    t = (text or "").strip()
    if not t:
        return {"chars": 0, "fillers": 0, "filler_ratio": 0.0,
                "formal_density": 0.0, "avg_sentence_len": 0.0, "sentences": 0}

    filler_hits = sum(t.count(f) for f in FILLERS)
    formal_hits = sum(t.count(m) for m in FORMAL_MARKERS)

    # 中文按标点断句；没有标点时按长度粗略切
    sentences = [s for s in re.split(r"[。！？!?；;\n]+", t) if s.strip()]
    if not sentences:
        sentences = [t]
    avg_len = sum(len(s) for s in sentences) / len(sentences)

    return {
        "chars": len(t),
        # 填充词按"每 100 字出现几次"归一，不同长度的回答才可比
        "fillers": filler_hits,
        "filler_ratio": round(filler_hits / max(len(t), 1) * 100, 3),
        "formal_density": round(formal_hits / max(len(sentences), 1), 3),
        "avg_sentence_len": round(avg_len, 2),
        "sentences": len(sentences),
    }


def pause_features(turns: list[dict], audio_events: list[dict]) -> dict:
    """聚合一场面试的停顿与语速。

    停顿来自 audio_event(kind=pause)，value 是毫秒。
    语速用"回答字数 / 回答音频时长"，单位 字/秒。
    """
    pauses = [e["value"] for e in audio_events if e["kind"] == "pause" and e.get("value")]
    speeds = [e["value"] for e in audio_events if e["kind"] == "speed" and e.get("value")]

    answered = [t for t in turns if t.get("answer")]
    total_answer_ms = sum(t.get("answer_audio_ms") or 0 for t in answered)
    total_chars = sum(len(t["answer"] or "") for t in answered)

    # 语速：优先用客户端上报的 speeds；缺失则用总量算
    if speeds:
        speed_avg = sum(speeds) / len(speeds)
    elif total_answer_ms > 0:
        speed_avg = total_chars / (total_answer_ms / 1000.0)
    else:
        speed_avg = 0.0

    # 停顿占比 = 总停顿时长 / (总停顿时长 + 总说话时长)
    pause_total = sum(pauses)
    denom = pause_total + total_answer_ms
    pause_ratio = (pause_total / denom) if denom > 0 else 0.0

    return {
        "pause_count": len(pauses),
        "pause_total_ms": int(pause_total),
        "pause_max_ms": int(max(pauses)) if pauses else 0,
        "pause_ratio": round(min(pause_ratio, 1.0), 4),
        "speed_chars_per_sec": round(speed_avg, 2) if speed_avg else 0.0,
        "speed_samples": len(speeds),
        "total_speech_ms": int(total_answer_ms),
        "total_chars": total_chars,
        # 语速稳定性：标准差 / 均值。越低越稳。
        "speed_cv": round(_cv(speeds), 4) if len(speeds) >= 2 else None,
        # 书面语密度：跨全部回答的均值
        "formal_density": round(
            sum(text_features(t["answer"])["formal_density"] for t in answered) / len(answered), 3
        ) if answered else 0.0,
    }


def _cv(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return (var ** 0.5) / mean


def speed_stability_score(audio: dict) -> tuple[float | None, str]:
    """语速稳定性 → 0..100。返回 (分, 说明)。

    为什么用变异系数而不是绝对语速：绝对语速因人而异（有人天生快），
    但"忽快忽慢"是所有人都会觉得不稳的信号。所以只惩罚波动，不惩罚快慢。
    """
    cv = audio.get("speed_cv")
    if cv is None:
        return None, "语速样本少于 2 个，无法算稳定性"
    if cv <= 0.15:
        return 85.0, f"语速变异系数 {cv:.2f}，非常稳定"
    if cv <= 0.30:
        return 70.0, f"语速变异系数 {cv:.2f}，正常波动"
    if cv <= 0.50:
        return 55.0, f"语速变异系数 {cv:.2f}，波动偏大"
    return 35.0, f"语速变异系数 {cv:.2f}，忽快忽慢"


# ---------------------------------------------------------------------------
# Qwen-Omni 实时会话（完整实现，填 Key 后可用）
# ---------------------------------------------------------------------------
class OmniSession:
    """通义 Qwen-Omni 双向流会话。

    用法（服务端）：
        s = OmniSession(interview_id, on_text=..., on_audio=...)
        s.start()
        s.push_pcm(pcm16_bytes)   # 浏览器每 100ms 推一段 16k PCM
        ...
        s.stop()

    **回调线程注意**：dashscope 的回调在它自己的网络线程里触发，
    不要在回调里直接写 SQLite（会撞锁）。这里统一丢进 `_pending` 列表，
    由调用方在主循环里 flush。这个坑在 docs/DESIGN.md 里记了。
    """

    def __init__(self, interview_id: str, on_text=None, on_audio=None):
        self.interview_id = interview_id
        self.on_text = on_text or (lambda t, final: None)
        self.on_audio = on_audio or (lambda b: None)
        self.session = None
        self.started = False
        self.error: str | None = None
        self._pending: list[tuple[str, str]] = []

    def start(self) -> bool:
        try:
            from dashscope.audio.qwen_omni import (OmniRealtimeCallback,
                                                  OmniRealtimeConversation,
                                                  MultiModality)
            outer = self

            class _CB(OmniRealtimeCallback):
                def on_open(self):
                    pass

                def on_close(self, code, msg):
                    outer.started = False

                def on_event(self, msg):
                    # 只处理我们关心的几类事件，其余忽略
                    try:
                        obj = json.loads(msg) if isinstance(msg, str) else (msg or {})
                    except Exception:
                        return
                    typ = obj.get("type", "")
                    if typ in ("response.audio_transcript.delta",
                               "conversation.item.input_audio_transcription.delta"):
                        text = obj.get("delta") or ""
                        if text:
                            outer._pending.append(("partial", text))
                            outer.on_text(text, False)
                    elif typ in ("response.audio_transcript.done",
                                 "conversation.item.input_audio_transcription.completed"):
                        text = obj.get("transcript") or obj.get("text") or ""
                        if text:
                            outer._pending.append(("final", text))
                            outer.on_text(text, True)
                    elif typ == "response.audio.delta":
                        b64 = obj.get("delta")
                        if b64:
                            outer.on_audio(b64)

            # 模型与音色都从 settings 取，别写死。
            # 踩过的两个坑（都是 404/400，不是代码 bug）：
            #   1) qwen-omni-turbo-realtime 已下线，会话能建起来但立刻回
            #      "InvalidParameter: Model not exist"，看着像连上了其实没用；
            #   2) 不传 voice 时 SDK 发出的是 voice="null"，服务端报
            #      "Voice 'null' is not supported"。
            self.session = OmniRealtimeConversation(
                model=settings.omni_model,
                callback=_CB(),
                api_key=settings.dashscope_api_key,
            )
            self.session.connect()
            self.session.update_session(
                output_modalities=[MultiModality.TEXT],
                voice=settings.omni_voice,
                enable_input_audio_transcription=True,
                enable_turn_detection=True,
            )
            self.started = True
            return True
        except Exception as e:  # pragma: no cover
            self.error = str(e)
            self.started = False
            return False

    def push_pcm(self, pcm16: bytes) -> None:
        """推一段 16bit 单声道 PCM。采样率需与 update_session 里声明一致。"""
        if not self.started or self.session is None:
            return
        try:
            self.session.append_audio(base64.b64encode(pcm16).decode("ascii"))
        except Exception as e:  # pragma: no cover
            self.error = str(e)

    def ask(self, instructions: str) -> None:
        """让模型基于当前上下文说一句（用于"AI 出题"）。"""
        if not self.started or self.session is None:
            return
        try:
            self.session.create_response(instructions=instructions)
        except Exception as e:  # pragma: no cover
            self.error = str(e)

    def stop(self) -> None:
        try:
            if self.session is not None:
                self.session.end_session()
                self.session.close()
        except Exception:  # pragma: no cover
            pass
        self.started = False


# ---------------------------------------------------------------------------
# 服务端侧：把浏览器上报的转写/停顿落库
# ---------------------------------------------------------------------------
def record_transcript(iid: str, text: str, audio_ms: int, final: bool = True) -> None:
    if not text:
        return
    db.add_audio_event(iid, "transcript", text=text,
                       meta={"audio_ms": audio_ms, "final": final})


def record_pause(iid: str, pause_ms: float) -> None:
    if pause_ms and pause_ms > 0:
        db.add_audio_event(iid, "pause", value=float(pause_ms))


def record_speed(iid: str, chars_per_sec: float) -> None:
    if chars_per_sec and chars_per_sec > 0:
        db.add_audio_event(iid, "speed", value=float(chars_per_sec))
