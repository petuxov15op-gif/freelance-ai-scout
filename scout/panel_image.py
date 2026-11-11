"""Visible light-blue dashboard card for Telegram clients without HTML Widgets."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


INK = "#14233d"
BLUE = "#1978f5"
MUTED = "#536a91"


def _font(size: int, bold: bool = False):
    names = (["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
             if bold else ["/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default(size=size)


def render_panel(state: dict) -> bytes:
    """Render existing dashboard counts as a single readable Telegram photo."""
    w, h = 680, 1010
    image = Image.new("RGB", (w, h), "#eaf5ff")
    d = ImageDraw.Draw(image)
    for y in range(h):
        d.line((0, y, w, y), fill=(236 - y * 8 // h, 246 - y * 3 // h, 255))
    d.ellipse((-80, 120, 470, 400), fill="#d9ebff")
    d.ellipse((280, 95, 780, 380), fill="#c9e1ff")
    d.ellipse((-20, 165, 720, 440), fill="#edf7ff")

    regular, bold = _font(25), _font(27, True)
    small, number = _font(22), _font(43, True)
    d.text((86, 36), "дашборд", font=_font(32, True), fill=INK)
    for gx in (45, 62):
        for gy in (43, 60):
            d.rounded_rectangle((gx, gy, gx + 11, gy + 11), radius=2, outline=INK, width=2)
    d.text((44, 89), "Freelance AI Scout", font=regular, fill=MUTED)

    def card(box, color="#f7fbff"):
        shadow = Image.new("RGBA", (w, h))
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle((box[0], box[1] + 9, box[2], box[3] + 9), radius=29, fill=(64, 114, 195, 53))
        image.paste(Image.alpha_composite(image.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(17))).convert("RGB"))
        ImageDraw.Draw(image).rounded_rectangle(box, radius=29, fill=color, outline="#ffffff", width=3)

    metrics = [("Новых", state["n"]), ("Подходят", state["m"]),
               ("Сохранено", state["s"]), ("Откликнулся", state["a"])]
    for index, (label, value) in enumerate(metrics):
        col, row = index % 2, index // 2
        x, y = 34 + col * 316, 285 + row * 170
        card((x, y, x + 296, y + 150))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((x + 25, y + 21, x + 51, y + 48), radius=6, outline=BLUE, width=3)
        if index == 1:
            draw.ellipse((x + 31, y + 27, x + 45, y + 41), outline=BLUE, width=2)
        elif index == 2:
            draw.line((x + 31, y + 37, x + 38, y + 43, x + 46, y + 32), fill=BLUE, width=3)
        draw.text((x + 24, y + 56), str(value), font=number, fill=INK)
        draw.text((x + 24, y + 112), label, font=small, fill=INK)

    card((34, 635, 646, 735))
    d = ImageDraw.Draw(image)
    d.ellipse((59, 659, 86, 686), outline="#18bc84", width=4)
    d.ellipse((67, 667, 78, 678), fill="#18bc84")
    d.text((105, 649), str(state["c"]), font=_font(38, True), fill=INK)
    d.text((105, 696), "Источников", font=small, fill=MUTED)
    for i, height in enumerate((21, 30, 39, 28, 52, 43, 59)):
        x = 466 + i * 21
        d.rounded_rectangle((x, 715 - height, x + 13, 715), radius=5, fill="#86bdff")

    card((34, 756, 646, 924))
    d = ImageDraw.Draw(image)
    d.text((60, 777), "Мониторинг", font=bold, fill=INK)
    d.text((60, 826), str(state["mon"])[:42], font=small, fill=MUTED)
    status = "Идёт" if state["running"] else "Активен" if state["c"] else "Нет источников"
    d.rounded_rectangle((466, 775, 619, 822), radius=23, fill="#dceaff")
    d.text((487, 784), status, font=_font(20, True), fill=BLUE)
    d.text((42, 951), "Данные обновляются при открытии панели", font=_font(19), fill=MUTED)
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
