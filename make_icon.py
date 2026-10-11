# -*- coding: utf-8 -*-
"""生成程序图标 app.ico（蓝底 + 硬盘柱体 + 容量条）。运行： python make_icon.py"""
import os

from PIL import Image, ImageDraw

SIZE = 256
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")


def rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def vertical_gradient(size, top, bottom):
    img = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        img.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img.resize((size, size))


def main():
    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    grad = vertical_gradient(SIZE, (58, 140, 240), (16, 62, 140)).convert("RGBA")
    base.paste(grad, (0, 0), rounded_mask(SIZE, 52))

    d = ImageDraw.Draw(base)
    edge = (150, 190, 240, 255)
    body = (222, 234, 250, 255)
    top = (255, 255, 255, 255)
    port = (74, 163, 255, 255)

    # 硬盘柱体
    x0, x1 = 58, 198
    top_box = (x0, 54, x1, 112)
    d.rectangle((x0, 83, x1, 172), fill=body)
    d.ellipse((x0, 144, x1, 198), fill=(198, 216, 240, 255), outline=edge, width=3)
    d.ellipse(top_box, fill=top, outline=edge, width=3)
    d.ellipse((x0 + 34, 74, x1 - 34, 94), fill=(232, 240, 251, 255), outline=(190, 210, 235, 255), width=2)

    # 容量条（体现"排行/占比"的含义）
    bar_x = 74
    widths = (100, 74, 52)
    y = 122
    for w in widths:
        d.rounded_rectangle((bar_x, y, bar_x + w, y + 12), radius=6, fill=port)
        y += 20

    base.save(OUT, format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("已生成图标: %s (%.1f KB)" % (OUT, os.path.getsize(OUT) / 1024.0))


if __name__ == "__main__":
    main()
