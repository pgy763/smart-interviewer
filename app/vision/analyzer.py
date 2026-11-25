"""本地视觉分析：不依赖任何模型，纯 Pillow + numpy 的启发式 CV。

**为什么先做启发式，而不是直接调视觉大模型**
1. 真实可用性：这个 demo 要能在没有 API Key 的机器上跑起来，实时视频分析
   如果全靠云端，第一次调用失败整个页面就废了。
2. 隐私与成本：逐帧上传人脸到云端，既慢又贵，还涉及个人信息。启发式在本地
   算出"有没有人脸、在不在画面中央、动得多不多"，只有需要语义判断时才调 VL 模型。
3. 接口留好：`VisionResult` 的字段与 Qwen-VL 的判定结果对齐，之后可以把
   `analyze_frame()` 换成云端实现，上层（scoring/patterns）一行不用改。

**这套启发式到底在算什么**（面试要能讲清楚，不能吹成"AI 识别情绪"）
- 人脸：YCbCr 空间的肤色阈值 + 4x4 降采样后的连通域，取面积最大的一块。
  这是 1990 年代的经典方法，对光照敏感，所以同时算了 brightness 作为置信度参考。
- 看镜头：人脸框中心相对画面中心的水平偏移。阈值 ±0.18 以内算"朝向镜头"。
  这是个**代理指标**——真正的视线估计需要虹膜检测或模型，这里只用位置近似，
  报告里必须写明这是 approximation，不能当成"视线追踪"。
- 动作幅度：相邻两帧小图（32x24 灰度）的平均绝对差。用来衡量"是否坐得住"，
  剧烈变化可能是离开座位或者大幅手势。

**明确的局限（写进报告，不藏）**
- 侧脸、戴眼镜反光、深色皮肤在强逆光下会漏检。
- 低头看稿 vs 低头思考，本方法无法区分——所以读稿风险只能给"可疑"，不能定性。
"""

from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass, asdict

import numpy as np
from PIL import Image, ImageFilter

# 肤色阈值（YCbCr）。取值参考公开的肤色检测经验区间，偏向宽松以减少漏检，
# 代价是木色桌面/米色墙面可能误检——所以后面还要加"面积占比"与"居中"约束。
SKIN_CR = (133, 173)
SKIN_CB = (77, 127)

# 分析用的降采样尺寸：越小人脸定位越粗，但 CPU 友好。64x48 在 30fps 下几乎无感。
WORK_W, WORK_H = 64, 48
# 动作幅度用的更小尺寸
MOTION_W, MOTION_H = 32, 24

# 看镜头的水平偏移阈值（归一化到 -1..1）
LOOK_DX_THRESHOLD = 0.18


@dataclass
class VisionResult:
    face_found: bool
    face_ratio: float          # 人脸框面积 / 画面面积，0..1
    center_dx: float           # -1..1，正数表示人脸偏右
    center_dy: float
    brightness: float          # 0..1
    looking: bool              # 是否判定为朝向镜头
    motion: float              # 0..1 粗略动作幅度
    note: str = ""

    def to_dict(self) -> dict:
        # 不能直接 asdict：字段里有 numpy 标量（np.float64 / np.True_），
        # Starlette 的 JSONResponse 用标准 json.dumps，不认识它们会抛
        # `TypeError: Object of type bool is not JSON serializable`。
        # 这里统一转成 Python 原生类型，让这个 dataclass 可以安全地直接序列化。
        return {
            "face_found": bool(self.face_found),
            "face_ratio": float(self.face_ratio),
            "center_dx": float(self.center_dx),
            "center_dy": float(self.center_dy),
            "brightness": float(self.brightness),
            "looking": bool(self.looking),
            "motion": float(self.motion),
            "note": str(self.note),
        }


def _decode(image_b64: str) -> Image.Image | None:
    """解码前端传来的 data URI 或裸 base64。"""
    if not image_b64:
        return None
    s = image_b64
    if "," in s and s.strip().startswith("data:"):
        s = s.split(",", 1)[1]
    try:
        raw = base64.b64decode(s, validate=False)
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


def _largest_skin_blob(img: Image.Image) -> tuple[tuple[int, int, int, int] | None, np.ndarray]:
    """在降采样图上找最大的肤色连通块，返回 bbox 与工作图尺寸。"""
    small = img.resize((WORK_W, WORK_H), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.float32)

    # RGB -> YCbCr（ITU-R BT.601），手工算避免额外依赖
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b

    mask = (
        (cr >= SKIN_CR[0]) & (cr <= SKIN_CR[1])
        & (cb >= SKIN_CB[0]) & (cb <= SKIN_CB[1])
        & (y > 45)  # 全黑区域不算肤色
    )

    # 形态学开运算（先腐蚀后膨胀）去噪：用 Pillow 的 MinFilter/MaxFilter 实现
    m_img = Image.fromarray((mask * 255).astype(np.uint8), mode="L")
    m_img = m_img.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    mask = np.asarray(m_img) > 127

    # 连通域：在 64x48 的小图上做 BFS，代价可忽略
    visited = np.zeros_like(mask, dtype=bool)
    best = None
    best_area = 0
    h, w = mask.shape
    for sy in range(h):
        for sx in range(w):
            if not mask[sy, sx] or visited[sy, sx]:
                continue
            # 迭代式 flood fill（避免递归深度问题）
            stack = [(sy, sx)]
            visited[sy, sx] = True
            area = 0
            minx = maxx = sx
            miny = maxy = sy
            while stack:
                cy, cx = stack.pop()
                area += 1
                if cx < minx: minx = cx
                if cx > maxx: maxx = cx
                if cy < miny: miny = cy
                if cy > maxy: maxy = cy
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))
            if area > best_area:
                best_area = area
                best = (minx, miny, maxx, maxy)
    return best, np.array([WORK_H, WORK_W])


def analyze_frame(image_b64: str, prev_small: np.ndarray | None = None
                  ) -> tuple[VisionResult, np.ndarray | None]:
    """分析一帧。

    返回 (结果, 供下一帧算动作幅度用的小图)。小图由调用方保存，
    这样无状态函数也能算帧间差异。
    """
    img = _decode(image_b64)
    if img is None:
        return VisionResult(False, 0.0, 0.0, 0.0, 0.0, False, 0.0,
                            "图像解码失败"), prev_small

    # 亮度：HSV 的 V 通道均值，用来提示"光线太暗，人脸检测不可靠"
    gray = img.convert("L")
    brightness = float(np.asarray(gray, dtype=np.float32).mean() / 255.0)

    # 动作幅度：与上一帧的小图做差
    small_m = np.asarray(gray.resize((MOTION_W, MOTION_H), Image.BILINEAR), dtype=np.float32)
    motion = 0.0
    if prev_small is not None and prev_small.shape == small_m.shape:
        motion = float(np.abs(small_m - prev_small).mean() / 255.0)

    bbox, shape = _largest_skin_blob(img)
    h, w = shape
    if bbox is None:
        result = VisionResult(False, 0.0, 0.0, 0.0, brightness, False, motion,
                              "未检测到肤色连通块")
        return result, small_m

    minx, miny, maxx, maxy = bbox
    bw = maxx - minx + 1
    bh = maxy - miny + 1
    face_ratio = (bw * bh) / float(w * h)

    # 面积太小视为噪声（<0.6% 画面），太大（>60%）多半是误检整面墙
    if face_ratio < 0.006:
        return VisionResult(False, face_ratio, 0.0, 0.0, brightness, False, motion,
                            "肤色块过小，判为噪声"), small_m
    if face_ratio > 0.60:
        return VisionResult(False, face_ratio, 0.0, 0.0, brightness, False, motion,
                            "肤色块过大，判为误检"), small_m

    cx = (minx + maxx) / 2.0 / (w - 1)   # 0..1
    cy = (miny + maxy) / 2.0 / (h - 1)
    center_dx = (cx - 0.5) * 2.0          # -1..1
    center_dy = (cy - 0.5) * 2.0

    # 朝向镜头：人脸框中心接近画面中心。这是代理指标，不是真实视线追踪。
    looking = abs(center_dx) <= LOOK_DX_THRESHOLD and abs(center_dy) <= 0.45

    note = "居中" if looking else ("偏左" if center_dx < 0 else "偏右")
    if brightness < 0.18:
        note += "（画面偏暗，检测可能不可靠）"
    return VisionResult(True, face_ratio, center_dx, center_dy, brightness,
                        looking, motion, note), small_m


# ---------------------------------------------------------------------------
# 聚合：把逐帧结果压成一场面试的时间序列指标
# ---------------------------------------------------------------------------
def summarize_frames(frames: list[dict]) -> dict:
    """把 vision_frame 表里的行聚合成报告需要的指标。

    **关键取舍：比例只按"检测到人脸的那些帧"做分母。**
    否则候选人偏头看稿时会因为"没检测到脸"被算成"没看镜头"，两次叠加惩罚。
    但"完全没有人脸"的时长单独统计成 away_ratio —— 那是"离开画面"，
    和"没看镜头"是两件事，混在一起面试官会误判。
    """
    if not frames:
        # 返回**完整的键集合**，只是值为 None/0，而不是一个只有 3 个键的瘦 dict。
        # 第一版这里返回 {"frames":0,"available":False,"reason":...}，结果报告模板
        # 渲染 `r.risk.vision.looking_ratio` 时抛
        # `jinja2.exceptions.UndefinedError: 'dict object' has no attribute 'looking_ratio'`
        # ——报告页直接 500。模板不该知道"有没有数据"这件事有几种形状，
        # 缺失就用 None 表示，展示层统一渲染成「—」。
        return {
            "frames": 0, "available": False, "with_face": 0,
            "reason": "没有收到任何视频帧（可能未授权摄像头或中途断开）",
            "face_ratio_avg": 0.0, "looking_ratio": None, "away_ratio": None,
            "longest_away_sec": 0.0, "median_frame_gap_sec": 0.0,
            "motion_avg": 0.0, "motion_p90": 0.0, "brightness_avg": 0.0, "dark_ratio": 0.0,
        }

    total = len(frames)
    with_face = [f for f in frames if f.get("face_found")]
    looking = [f for f in with_face if f.get("looking")]
    away = total - len(with_face)

    # 相邻帧的时间间隔用来判断"离开时长"，取中位间隔做基准
    ts = [f["ts"] for f in frames]
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    median_gap = float(np.median(gaps)) if gaps else 0.0

    # 最长连续无脸片段
    longest_away = 0.0
    run_start = None
    for f in frames:
        if not f.get("face_found"):
            if run_start is None:
                run_start = f["ts"]
        else:
            if run_start is not None:
                longest_away = max(longest_away, f["ts"] - run_start)
                run_start = None
    if run_start is not None:
        longest_away = max(longest_away, ts[-1] - run_start)

    motions = [f.get("motion") or 0.0 for f in frames]
    brights = [f.get("brightness") or 0.0 for f in frames]

    return {
        "frames": total,
        "available": True,
        "face_ratio_avg": round(float(np.mean([f.get("face_ratio") or 0 for f in with_face])), 4)
        if with_face else 0.0,
        # 看镜头比例：分母是"有脸的帧"，这是上面解释的取舍
        "looking_ratio": round(len(looking) / len(with_face), 4) if with_face else None,
        # 离开画面比例：分母是全部帧
        "away_ratio": round(away / total, 4),
        "longest_away_sec": round(longest_away, 2),
        "median_frame_gap_sec": round(median_gap, 3),
        "motion_avg": round(float(np.mean(motions)), 4),
        "motion_p90": round(float(np.percentile(motions, 90)), 4),
        "brightness_avg": round(float(np.mean(brights)), 4),
        "dark_ratio": round(sum(1 for b in brights if b < 0.18) / total, 4),
    }


def script_risk_score(summary: dict, audio: dict) -> tuple[float, list[str]]:
    """读稿/外部提示风险的启发式打分，返回 (0..1, 理由列表)。

    **必须理解这只是"可疑度"，不是"作弊判定"。** 三个信号各自都可能是良性原因：
    - 视线长期偏离：可能在看自己写的笔记提纲（很多面试允许）
    - 长停顿后突然流利：可能是思考后组织好了语言
    - 书面语密度异常：可能是这个人平时就这么说话

    所以报告里永远写"风险提示"，并且要求人工复核。这一条在 docs/DESIGN.md
    里专门写了"为什么不给确定性结论"。
    """
    reasons: list[str] = []
    score = 0.0

    looking = summary.get("looking_ratio")
    if looking is not None:
        off = 1.0 - looking
        # 偏离超过 35% 才开始计分，避免把正常思考算成读稿
        if off > 0.35:
            s = min((off - 0.35) / 0.55, 1.0) * 0.55
            score += s
            reasons.append(f"视线偏离镜头占比 {off:.0%}")

    if summary.get("longest_away_sec", 0) > 8:
        score += 0.15
        reasons.append(f"最长连续离开画面 {summary['longest_away_sec']:.1f} 秒")

    if audio:
        pause_ratio = audio.get("pause_ratio") or 0
        if pause_ratio > 0.45:
            score += 0.12
            reasons.append(f"停顿占比 {pause_ratio:.0%} 偏高")
        formal = audio.get("formal_density") or 0
        if formal > 0.25:
            score += 0.18
            reasons.append(f"书面语密度 {formal:.0%} 异常（口语通常低于 15%）")

    return min(score, 1.0), reasons
