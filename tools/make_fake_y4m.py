"""生成一段"人脸"假视频（y4m），喂给 headless Edge 的假摄像头。

为什么要这个：Edge 的 `--use-fake-device-for-media-stream` 给的是一个绿色
测试图案（pacman），里面没有任何肤色区域，于是视觉启发式会判"未检测到人脸"，
截图里显示「离开画面 Ns」——那是**正确行为**，但拿去做演示图会让人误以为
摄像头链路坏了。

用 `--use-file-for-fake-video-capture=fake_face.y4m` 换成一个合成的
"人形画面"：中间一个椭圆肤色块 + 两只眼睛，正好落在这套启发式
（YCbCr 肤色连通域 + 中心偏移）能识别的范围内，于是徽标显示「在画面内」。

注意这只是**测试替身**：它证明"抓帧 → 上传 → 服务端分析 → 落库 → 徽标"
这条链路是通的，不证明真人识别准确率。

运行：python tools/make_fake_y4m.py
输出：tools/fake_face.y4m（默认 8 秒 × 15fps）
"""

from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageDraw

W, H = 320, 240
FPS = 15
SECONDS = 6
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_face.y4m")

# 肤色（和 app/vision/analyzer.py 里的 SKIN_CR/SKIN_CB 同一族）
SKIN = (222, 178, 148)


def face_frame(shift_x: float = 0.0) -> Image.Image:
    """画一帧：浅灰背景 + 肤色椭圆脸 + 眼睛 + 嘴巴。"""
    img = Image.new("RGB", (W, H), (232, 234, 238))
    d = ImageDraw.Draw(img)
    cx = W / 2 + shift_x * W * 0.06
    cy = H / 2
    # 脸的尺寸按画面比例算，不能写死像素——写死的话改分辨率就会溢出整个画面，
    # 肤色块占比超过 60%，启发式（正确地）判成"肤色块过大=误检"。
    rx = W * 0.148          # 半宽 ≈ 14.8% 画面宽
    ry = H * 0.26           # 半高 ≈ 26% 画面高 → 面积占比约 12%，落在 0.6%~60% 中间
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=SKIN)
    # 眼睛（深色，避免被算进肤色块）
    d.ellipse([cx - rx * 0.55, cy - ry * 0.36, cx - rx * 0.21, cy - ry * 0.11], fill=(70, 62, 58))
    d.ellipse([cx + rx * 0.21, cy - ry * 0.36, cx + rx * 0.55, cy - ry * 0.11], fill=(70, 62, 58))
    # 嘴
    d.arc([cx - rx * 0.46, cy + ry * 0.14, cx + rx * 0.46, cy + ry * 0.66],
          start=20, end=160, fill=(120, 80, 76), width=max(2, W // 128))
    return img


def to_yuv420(img: Image.Image) -> bytes:
    """RGB → 平面 YUV420（y4m 的 C420jpeg）。"""
    arr = np.asarray(img, dtype=np.float32)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    u = -0.168736 * r - 0.331264 * g + 0.5 * b + 128.0
    v = 0.5 * r - 0.418688 * g - 0.081312 * b + 128.0
    y = np.clip(y, 0, 255).astype(np.uint8)
    # 4:2:0 —— U/V 各减半采样
    u = np.clip(u, 0, 255).astype(np.uint8)[::2, ::2]
    v = np.clip(v, 0, 255).astype(np.uint8)[::2, ::2]
    return y.tobytes() + u.tobytes() + v.tobytes()


def main() -> None:
    header = f"YUV4MPEG2 W{W} H{H} F{FPS}:1 Ip A1:1 C420jpeg\n"
    with open(OUT, "wb") as f:
        f.write(header.encode("ascii"))
        total = FPS * SECONDS
        for i in range(total):
            # 轻微左右摆动，让帧间 motion 不为 0（否则"动作幅度"恒为 0 看着像没采到）
            shift = np.sin(i / total * 2 * np.pi) * 0.6
            img = face_frame(float(shift))
            f.write(b"FRAME\n")
            f.write(to_yuv420(img))
    size = os.path.getsize(OUT)
    print(f"写出 {OUT}（{size} 字节，{SECONDS}s @ {FPS}fps，{W}x{H}）")


if __name__ == "__main__":
    main()
