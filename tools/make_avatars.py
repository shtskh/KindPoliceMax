# -*- coding: utf-8 -*-
"""
Аватарки для чата модерации: плоская двухмерная графика.

Рисуем фигурами, а не генерируем нейросетью: аватарка в мессенджере
видна размером около ста точек, и там читаются только крупные пятна.
Генератор же выдаёт мелкие детали, которые в кружке превращаются в кашу.

Холст рисуется вчетверо крупнее и уменьшается — так получаются ровные
края без зубцов.
"""
import math
import pathlib

from PIL import Image, ImageDraw

SIZE = 1024
S = 4  # во столько раз крупнее рисуем
OUT = pathlib.Path("assets/avatars")

NAVY = (22, 48, 92)
NAVY_DEEP = (13, 29, 58)
CROWN = (42, 77, 143)
CROWN_LIGHT = (56, 95, 168)
VISOR = (12, 24, 48)
GOLD = (232, 179, 58)
GOLD_DARK = (182, 134, 32)
CREAM = (245, 242, 232)
WHITE = (255, 255, 255)


def px(*values):
    """Переводит координаты холста в координаты увеличенного холста."""
    return tuple(int(round(v * S)) for v in values)


def new_canvas(background):
    image = Image.new("RGB", (SIZE * S, SIZE * S), background)
    return image, ImageDraw.Draw(image)


def finish(image, name):
    OUT.mkdir(parents=True, exist_ok=True)
    small = image.resize((SIZE, SIZE), Image.LANCZOS)
    path = OUT / name
    small.save(path, "PNG")
    print("готово:", path)
    return path


def bezier(points, steps=60):
    """Квадратичная кривая по трём точкам — для плавных обводов."""
    (x0, y0), (x1, y1), (x2, y2) = points
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append((u * u * x0 + 2 * u * t * x1 + t * t * x2,
                    u * u * y0 + 2 * u * t * y1 + t * t * y2))
    return out


def draw_cap(draw, cx, cy, width, *, crown=CROWN, crown_top=CROWN_LIGHT,
             band=NAVY_DEEP, visor=VISOR, cockade=True):
    """
    Форменная фуражка анфас: тулья, околыш, козырёк, кокарда.

    Узнаёт её глаз именно по пропорциям: широкая плоская тулья нависает
    над узким околышем. Полукруглый купол — это уже котелок, а не
    форменный головной убор.

    cx, cy — середина околыша; width — ширина тульи сверху.
    """
    band_w = width * 0.76
    top_h = width * 0.17
    wall_h = width * 0.25
    band_h = width * 0.15
    visor_w = band_w * 1.14
    visor_h = width * 0.16

    band_top = cy - band_h / 2
    band_bottom = cy + band_h / 2
    top_y = band_top - wall_h

    # Козырёк — первым: верхним краем он уходит под околыш.
    draw.chord(px(cx - visor_w / 2, band_bottom - visor_h,
                  cx + visor_w / 2, band_bottom + visor_h),
               0, 180, fill=visor)

    # Боковины тульи: кверху расширяются.
    draw.polygon([px(cx - width / 2, top_y), px(cx + width / 2, top_y),
                  px(cx + band_w / 2, band_top + 1),
                  px(cx - band_w / 2, band_top + 1)], fill=crown)

    # Плоский верх тульи.
    draw.ellipse(px(cx - width / 2, top_y - top_h / 2,
                    cx + width / 2, top_y + top_h / 2), fill=crown_top)

    # Околыш.
    draw.rounded_rectangle(
        px(cx - band_w / 2, band_top, cx + band_w / 2, band_bottom),
        radius=int(band_h * S * 0.22), fill=band)

    if cockade:
        r = width * 0.062
        draw.ellipse(px(cx - r, cy - r, cx + r, cy + r), fill=GOLD)
        draw.ellipse(px(cx - r * 0.42, cy - r * 0.42,
                        cx + r * 0.42, cy + r * 0.42), fill=NAVY_DEEP)


def draw_check(draw, cx, cy, size, color, width_ratio=0.22):
    """Галочка одной толстой линией со скруглёнными концами."""
    points = [(cx - size * 0.52, cy + size * 0.02),
              (cx - size * 0.14, cy + size * 0.40),
              (cx + size * 0.54, cy - size * 0.42)]
    w = int(size * width_ratio * S)
    draw.line([px(*p) for p in points], fill=color, width=w, joint="curve")
    for point in (points[0], points[2]):
        draw.ellipse(px(point[0] - size * width_ratio / 2,
                        point[1] - size * width_ratio / 2,
                        point[0] + size * width_ratio / 2,
                        point[1] + size * width_ratio / 2), fill=color)


# --------------------------------------------------------------------------
# Вариант 1: фуражка
# --------------------------------------------------------------------------

def avatar_cap():
    image, draw = new_canvas(NAVY)

    # Тонкое золотое кольцо по краю: в круглой обрезке мессенджера оно
    # очерчивает аватарку и отделяет её от фона переписки.
    draw.ellipse(px(28, 28, SIZE - 28, SIZE - 28),
                 outline=GOLD, width=int(7 * S))

    draw_cap(draw, SIZE / 2, 582, 600)
    return finish(image, "moderation-cap.png")


# --------------------------------------------------------------------------
# Вариант 2: щит с галочкой
# --------------------------------------------------------------------------

def avatar_shield():
    image, draw = new_canvas(NAVY)

    top, left, right, tip = 250, 270, 754, 820
    shoulder = 560
    outline = (
        [(left, top + 40)]
        + bezier([(left, top + 40), (left, top), (left + 46, top)])
        + [(right - 46, top)]
        + bezier([(right - 46, top), (right, top), (right, top + 40)])
        + [(right, shoulder)]
        + bezier([(right, shoulder), (right, tip - 90), (SIZE / 2, tip)])
        + bezier([(SIZE / 2, tip), (left, tip - 90), (left, shoulder)])
    )
    draw.polygon([px(*p) for p in outline], fill=GOLD)

    # Внутренняя линия: без неё щит выглядит просто жёлтым пятном.
    inner = [((x - SIZE / 2) * 0.86 + SIZE / 2,
              (y - 520) * 0.86 + 520) for x, y in outline]
    draw.line([px(*p) for p in inner + inner[:1]],
              fill=GOLD_DARK, width=int(6 * S), joint="curve")

    draw_check(draw, SIZE / 2, 510, 300, NAVY)
    return finish(image, "moderation-shield.png")


# --------------------------------------------------------------------------
# Вариант 3: реплика в фуражке
# --------------------------------------------------------------------------

def avatar_bubble():
    image, draw = new_canvas(NAVY)

    # Хвостик реплики рисуем до самого облака, чтобы стык не был виден.
    draw.polygon([px(395, 690), px(352, 862), px(520, 712)], fill=CREAM)
    draw.rounded_rectangle(px(226, 392, 798, 716), radius=int(104 * S),
                           fill=CREAM)

    # Глаза и улыбка: лицо делает аватарку живой, а не значком.
    for ex in (424, 600):
        draw.ellipse(px(ex - 26, 520, ex + 26, 580), fill=NAVY_DEEP)
    draw.arc(px(438, 560, 586, 660), 20, 160,
             fill=NAVY_DEEP, width=int(16 * S))

    draw_cap(draw, SIZE / 2, 392, 360)
    return finish(image, "moderation-bubble.png")


if __name__ == "__main__":
    avatar_cap()
    avatar_shield()
    avatar_bubble()
