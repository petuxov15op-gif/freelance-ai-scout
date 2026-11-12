"""Deterministic graphite order artwork; actions are real Telegram buttons."""
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from threading import Lock

from PIL import Image, ImageDraw, ImageFont

FONT = Path(__file__).parent / "assets/fonts/Inter.ttf"
WIDTH = 1080
PAD = 48
TEXT = "#f2f1f4"
MUTED = "#aaa8b3"
ACCENT = "#c9bedb"
BG = "#17171d"
LINE = "#3b3b46"
RENDER_LOCK = Lock()


@lru_cache(maxsize=32)
def font(size, weight=400):
    face = ImageFont.truetype(str(FONT), size)
    axes = face.get_variation_axes()
    face.set_variation_by_axes([weight if axis["name"] == b"Weight" else axis["default"] for axis in axes])
    return face


def lines(text, face, width, maximum=3):
    """Wrap at pixel boundaries, including unbroken URLs and long words."""
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    result = []
    for paragraph in str(text or "").splitlines():
        current = ""
        for word in paragraph.split():
            candidate = (current + " " + word).strip()
            if draw.textlength(candidate, font=face) <= width:
                current = candidate
                continue
            if current:
                result.append(current)
            current = ""
            for char in word:
                if current and draw.textlength(current + char, font=face) > width:
                    result.append(current)
                    current = ""
                current += char
        result.append(current)
    if len(result) > maximum:
        result = result[:maximum]
        last = result[-1]
        while last and draw.textlength(last + "…", font=face) > width:
            last = last[:-1]
        result[-1] = last.rstrip(" .") + "…"
    return result or [""]


def render_order_image(data):
    # FreeType faces are cached; serialize access across async worker threads.
    with RENDER_LOCK:
        return _render_order_image(data)


def _render_order_image(data):
    title = lines(data["title"], font(58, 650), WIDTH - PAD * 2, 3)
    summary = lines(data.get("summary", ""), font(34), WIDTH - PAD * 2, 3)
    compact = data.get("view", "home") != "home"
    if compact:
        title = lines(data["title"], font(46, 600), WIDTH - PAD * 2, 2)
        height = 270 + len(title) * 60
    else:
        extras = []
        if data.get("amount"):
            extras += lines("Моя цена: " + data["amount"], font(30), WIDTH - PAD * 2, 2)
        if data.get("note"):
            extras += lines("Заметка: " + data["note"], font(30), WIDTH - PAD * 2, 3)
        if data.get("notice"):
            extras += lines(data["notice"], font(30), WIDTH - PAD * 2, 3)
        height = 148 + len(title) * 74 + 28 + len(summary) * 46 + 36 + 150 + 100 + 58
        height += len(extras) * 42 + (24 if extras else 0)
    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((1, 1, WIDTH - 2, height - 2), radius=30, fill=BG, outline=LINE, width=2)
    draw.rounded_rectangle((PAD, 30, PAD + 44, 74), radius=14, fill="#302b3d")
    draw.ellipse((PAD + 10, 40, PAD + 34, 64), outline=ACCENT, width=2)
    draw.ellipse((PAD + 18, 48, PAD + 26, 56), fill=ACCENT)
    draw.text((PAD + 60, 30), "Freelance AI Scout", font=font(30, 600), fill=TEXT)
    label = "ПРОБНАЯ КАРТОЧКА" if data.get("sample") else "КАРТОЧКА ЗАКАЗА"
    draw.text((WIDTH - PAD, 39), label, font=font(20, 500), fill=MUTED, anchor="ra")
    draw.line((PAD, 100, WIDTH - PAD, 100), fill=LINE, width=2)
    draw.text((PAD, 124), data["source"][:65], font=font(24, 500), fill=MUTED)
    y = 178
    for line in title:
        draw.text((PAD, y), line, font=font(46, 600) if compact else font(58, 650), fill=TEXT)
        y += 60 if compact else 74
    if compact:
        draw.text((PAD, y + 24), data.get("section", "Заказ"), font=font(28, 500), fill=ACCENT)
    else:
        y += 24
        for line in summary:
            draw.text((PAD, y), line, font=font(34), fill=MUTED)
            y += 46
        y += 36
        gap = 16
        tile_width = (WIDTH - PAD * 2 - gap * 2) // 3
        metrics = [("БЮДЖЕТ", data["budget"]), ("СРОК", data["deadline"]), ("ИИ-РЕЙТИНГ", data["score"])]
        for index, (label, value) in enumerate(metrics):
            x = PAD + index * (tile_width + gap)
            draw.rounded_rectangle((x, y, x + tile_width, y + 150), radius=18,
                                   fill="#302b3e" if index == 2 else "#232329", outline="#4a4359" if index == 2 else LINE, width=2)
            draw.text((x + 22, y + 22), label, font=font(21, 500), fill=MUTED)
            size = 44
            while size > 26 and draw.textlength(value, font=font(size, 600)) > tile_width - 44:
                size -= 2
            value = lines(value, font(size, 600), tile_width - 44, 1)[0]
            draw.text((x + 22, y + 68), value, font=font(size, 600), fill=ACCENT if index == 2 else TEXT)
        y += 184
        following = "Отслеживается" if data.get("following") else "Не отслеживается"
        state = following + "   ·   " + data["status"]
        state = lines(state, font(29, 500), WIDTH - PAD * 2, 1)[0]
        draw.text((PAD, y), state, font=font(29, 500), fill=ACCENT)
        y += 48
        draw.text((PAD, y), data["contact"][:70], font=font(25), fill=MUTED)
        y += 56
        for line in extras:
            draw.text((PAD, y), line, font=font(30), fill=TEXT)
            y += 42
    output = BytesIO()
    image.save(output, "PNG", optimize=True)
    return output.getvalue()
