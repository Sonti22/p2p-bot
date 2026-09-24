"""Картинки для бота: карточка связки, график топа, аватарка. Pillow + шрифты Windows.

Аватарка в файл:  python cards.py
"""
import io
import math
import os
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from p2p import _money, _price

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
BG, PANEL, BORDER = "#0F1419", "#18212C", "#2A3441"
TEXT, MUTED = "#E8EDF2", "#8B98A5"
GREEN, RED, AMBER, BLUE = "#22C55E", "#EF4444", "#F59E0B", "#3B82F6"
VENUE_COLORS = {"Bybit": "#F7A600", "MEXC": "#2D7FF9", "HTX": "#1F6FEB", "KuCoin": "#24AE8F",
                "BitPapa": "#7C4DFF", "BestChange": "#10B981"}


def _font(size, weight="regular"):
    name = {"regular": "segoeui.ttf", "semi": "seguisb.ttf", "bold": "segoeuib.ttf"}[weight]
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except OSError:
        return ImageFont.load_default(size)


def _fit(text, font, width):
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + "…") > width:
        text = text[:-1]
    return text + "…"


def _wrap(text, font, width):
    lines, cur = [], ""
    for word in text.split():
        nxt = f"{cur} {word}".strip()
        if font.getlength(nxt) <= width:
            cur = nxt
        else:
            if cur:
                lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _pill(d, x, y, text, color, font, pad=12):
    w = font.getlength(text) + pad * 2
    d.rounded_rectangle((x, y, x + w, y + font.size + 14), radius=(font.size + 14) // 2, fill=color)
    d.text((x + pad, y + 5), text, font=font, fill="#0B0F14")
    return x + w


def _side_box(d, x, y, w, h, title, color, ad):
    d.rounded_rectangle((x, y, x + w, y + h), radius=22, fill=PANEL, outline=BORDER, width=2)
    f_small, f_mid = _font(20, "semi"), _font(24)
    nx = _pill(d, x + 20, y + 18, title, color, f_small)
    _pill(d, nx + 10, y + 18, ad.ex, VENUE_COLORS.get(ad.ex, BLUE), f_small)
    d.text((x + 20, y + 66), f"{ad.asset} {_price(ad.price)} ₽", font=_font(40, "bold"), fill=TEXT)
    d.text((x + 20, y + 122), _fit(ad.nick, f_mid, w - 40), font=f_mid, fill=TEXT)
    stats = f"{ad.orders} отзывов · {ad.rate:.0f}% хор." if ad.ex == "BestChange" else f"{ad.orders} сделок · {ad.rate:.0f}%"
    d.text((x + 20, y + 156), stats, font=_font(22), fill=MUTED)
    d.text((x + 20, y + 190), _fit(", ".join(ad.pays), _font(22), w - 40), font=_font(22), fill=MUTED)


def _amounts_line(amounts):
    parts = []
    for amt, val in amounts.items():
        label = f"{amt // 1000}к"
        parts.append(f"{label} {val:+.2f}%" if val is not None else f"{label} —")
    return "На другую сумму: " + " · ".join(parts)


def deal_card(deal, cfg, amounts=None):
    profit, b, s, route = deal
    W, H = 1080, 640
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((40, 30), "P2P-СВЯЗКА", font=_font(24, "semi"), fill=MUTED)
    stamp = time.strftime("%d.%m %H:%M")
    d.text((W - 40 - _font(24).getlength(stamp), 30), stamp, font=_font(24), fill=MUTED)

    color = AMBER if profit >= 5 else GREEN if profit > 0 else RED
    big = f"{profit:+.2f}%"
    f_big = _font(96, "bold")
    d.text((36, 58), big, font=f_big, fill=color)
    d.text((56 + f_big.getlength(big), 108), f"чистыми на {_money(cfg.amount)} ₽", font=_font(30), fill=MUTED)
    d.text((40, 180), f"{b.ex} {b.asset}  →  {s.ex} {s.asset}", font=_font(30, "semi"), fill=TEXT)
    if amounts:
        d.text((40, 212), _fit(_amounts_line(amounts), _font(18), W - 80), font=_font(18), fill=MUTED)

    y, h = 240, 240
    _side_box(d, 40, y, 360, h, "КУПИТЬ", GREEN, b)
    _side_box(d, 680, y, 360, h, "ПРОДАТЬ", RED, s)
    # шаги маршрута столбиком (каждый шаг целиком, с номером) и стрелка под ними
    f_step, mid, colw = _font(19), 540, 250
    lines = []
    for i, step in enumerate(route.split(" → "), 1):
        wrapped = _wrap(step, f_step, colw - 30)
        lines += [(f"{i}. {wrapped[0]}" if len(route.split(" → ")) > 1 else wrapped[0], TEXT)] + [(w, TEXT) for w in wrapped[1:]]
    block = len(lines) * 25 + 34
    ty = y + (h - block) // 2
    for ln, color in lines[:7]:
        d.text((mid - f_step.getlength(ln) / 2, ty), ln, font=f_step, fill=color)
        ty += 25
    ay = ty + 16
    d.line((mid - 100, ay, mid + 90, ay), fill=MUTED, width=4)
    d.polygon([(mid + 104, ay), (mid + 86, ay - 12), (mid + 86, ay + 12)], fill=MUTED)

    d.rounded_rectangle((40, 520, W - 40, 600), radius=18, fill="#2A2112", outline="#5C4513", width=2)
    d.ellipse((62, 545, 92, 575), fill=AMBER)
    d.text((72, 544), "!", font=_font(24, "bold"), fill="#0B0F14")
    note = "Проверь ФИО отправителя и условия мерчанта. Первая сделка — малой суммой."
    if profit >= 5:
        note = "Спред ≥5% часто плата за риск: проверь мерчанта и ФИО, начни с малой суммы."
    d.text((108, 545), _fit(note, _font(22), W - 170), font=_font(22), fill="#F5D9A8")
    return _png(img)


def top_chart(snap, cfg, n=8):
    deals = snap.deals[:n]
    rows = max(len(deals), 1)
    W, H = 1080, 200 + rows * 84 + 80
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((40, 30), "Топ связок сейчас", font=_font(44, "bold"), fill=TEXT)
    sub = f"USDT {snap.ref:.2f} ₽ · круг {_money(cfg.amount)} ₽ · {time.strftime('%d.%m %H:%M')}"
    d.text((40, 96), sub, font=_font(24), fill=MUTED)
    d.line((40, 150, W - 40, 150), fill=BORDER, width=2)
    if not deals:
        d.text((40, 180), "Сейчас связок нет — все объявления отсеяны фильтрами.", font=_font(28), fill=MUTED)
        return _png(img)

    top = max(dd[0] for dd in deals) or 1
    bar_x0, bar_x1 = 560, 930
    for i, (profit, b, s, route) in enumerate(deals):
        y = 172 + i * 84
        d.text((40, y), _fit(f"{i + 1}. {b.ex} {b.asset} → {s.ex} {s.asset}", _font(26, "semi"), 500),
               font=_font(26, "semi"), fill=TEXT)
        d.text((64, y + 36), _fit(route, _font(19), 476), font=_font(19), fill=MUTED)
        color = AMBER if profit >= 5 else GREEN if profit >= 2 else BLUE if profit > 0 else RED
        length = max(8, (bar_x1 - bar_x0) * max(profit, 0) / top)
        d.rounded_rectangle((bar_x0, y + 10, bar_x0 + length, y + 50), radius=10, fill=color)
        d.text((bar_x1 + 16, y + 10), f"{profit:+.2f}%", font=_font(28, "bold"), fill=color)

    ly = H - 58
    for x, color, label in ((40, GREEN, "2–5% — рабочая зона"), (360, AMBER, "≥5% — часто плата за риск, проверь мерчанта")):
        d.rounded_rectangle((x, ly + 6, x + 22, ly + 28), radius=5, fill=color)
        d.text((x + 32, ly), label, font=_font(22), fill=MUTED)
    return _png(img)


def _hex(c, a=255):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) + (a,)


def _bg(S, c1, c2, kind="diag"):
    """Фон-градиент: diag — по диагонали, radial — от центра к краям."""
    if kind == "radial":
        mask = Image.radial_gradient("L").resize((S, S))
    else:
        mask = Image.linear_gradient("L").rotate(-45, expand=True).resize((S * 3 // 2, S * 3 // 2))
        off = (mask.width - S) // 2
        mask = mask.crop((off, off, off + S, off + S))
    return Image.composite(Image.new("RGBA", (S, S), _hex(c2)), Image.new("RGBA", (S, S), _hex(c1)), mask)


def _glow(base, layer, radius, strength=2):
    """Мягкое свечение: размытая копия слоя под чёткий слой."""
    g = layer.filter(ImageFilter.GaussianBlur(radius))
    for _ in range(strength):
        base = Image.alpha_composite(base, g)
    return Image.alpha_composite(base, layer)


def _arrow_arc(d, cx, cy, r, a0, a1, color, wid):
    d.arc((cx - r, cy - r, cx + r, cy + r), a0, a1, fill=color, width=wid)
    th = math.radians(a1)
    px, py = cx + (r - wid / 2) * math.cos(th), cy + (r - wid / 2) * math.sin(th)
    tx, ty, nx, ny = -math.sin(th), math.cos(th), math.cos(th), math.sin(th)
    L, W = wid * 1.7, wid * 1.35
    d.polygon([(px + tx * L, py + ty * L), (px + nx * W, py + ny * W), (px - nx * W, py - ny * W)], fill=color)


def _text_center(d, cx, y, text, font, fill):
    d.text((cx - font.getlength(text) / 2, y), text, font=font, fill=fill)


def _avatar_radar(S):
    cx = cy = S // 2
    base = _bg(S, "#0E3B38", "#03070B", "radial")
    grid = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(grid)
    for rf in (0.2, 0.3, 0.4):
        R = int(S * rf)
        d.ellipse((cx - R, cy - R, cx + R, cy + R), outline=(45, 212, 191, 70), width=max(2, S // 400))
    d.line((cx, int(S * 0.08), cx, int(S * 0.92)), fill=(45, 212, 191, 35), width=max(2, S // 640))
    d.line((int(S * 0.08), cy, int(S * 0.92), cy), fill=(45, 212, 191, 35), width=max(2, S // 640))
    base = Image.alpha_composite(base, grid)

    R, lead = int(S * 0.40), -40
    sweep = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sweep)
    for i in range(80):   # «хвост» луча радара, ярче к переднему краю
        sd.pieslice((cx - R, cy - R, cx + R, cy + R), lead - 80 + i, lead - 78.5 + i, fill=(34, 197, 94, int(2 + i * 1.9)))
    base = Image.alpha_composite(base, sweep.filter(ImageFilter.GaussianBlur(S // 300)))
    edge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    th = math.radians(lead)
    ImageDraw.Draw(edge).line((cx, cy, cx + R * math.cos(th), cy + R * math.sin(th)), fill=(167, 243, 208, 255), width=S // 150)
    base = _glow(base, edge, S // 60)

    blips = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(blips)
    for rf, ang, col in ((0.34, -70, AMBER), (0.37, -20, GREEN), (0.27, 130, "#22D3EE"), (0.36, 200, GREEN)):
        x, y, rr = cx + S * rf * math.cos(math.radians(ang)), cy + S * rf * math.sin(math.radians(ang)), S // 60
        bd.ellipse((x - rr, y - rr, x + rr, y + rr), fill=_hex(col))
    base = _glow(base, blips, S // 45, 3)

    plate = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    P = int(S * 0.2)
    ImageDraw.Draw(plate).ellipse((cx - P, cy - P, cx + P, cy + P), fill=(6, 14, 20, 240),
                                  outline=(45, 212, 191, 200), width=S // 200)
    base = _glow(base, plate, S // 50, 1)
    txt = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    td = ImageDraw.Draw(txt)
    _text_center(td, cx, cy - S * 0.115, "P2P", _font(int(S * 0.14), "bold"), (240, 253, 250, 255))
    _text_center(td, cx, cy + S * 0.055, "S C A N", _font(int(S * 0.042), "semi"), (94, 234, 212, 255))
    return _glow(base, txt, S // 90, 1)


def _avatar_neon(S):
    cx = cy = S // 2
    base = _bg(S, "#1B0736", "#011B36")
    stars = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stars)
    for fx, fy, k in ((0.2, 0.22, 1.0), (0.8, 0.3, 0.7), (0.74, 0.8, 0.9), (0.25, 0.75, 0.6), (0.52, 0.12, 0.5)):
        x, y, L = S * fx, S * fy, S * 0.018 * k
        sd.line((x - L, y, x + L, y), fill=(255, 255, 255, 180), width=max(2, S // 500))
        sd.line((x, y - L, x, y + L), fill=(255, 255, 255, 180), width=max(2, S // 500))
    base = _glow(base, stars, S // 200, 1)

    arcs = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ad = ImageDraw.Draw(arcs)
    r, wid = int(S * 0.36), int(S * 0.045)
    _arrow_arc(ad, cx, cy, r, 195, 320, _hex("#22D3EE"), wid)
    _arrow_arc(ad, cx, cy, r, 15, 140, _hex("#F472B6"), wid)
    base = _glow(base, arcs, S // 35, 3)

    mask = Image.new("L", (S, S), 0)
    _text_center(ImageDraw.Draw(mask), cx, cy - S * 0.15, "P2P", _font(int(S * 0.2), "bold"), 255)
    grad = Image.composite(Image.new("RGBA", (S, S), _hex("#F472B6")), Image.new("RGBA", (S, S), _hex("#22D3EE")),
                           Image.linear_gradient("L").rotate(90).resize((S, S)))
    txt = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    txt.paste(grad, (0, 0), mask)
    base = _glow(base, txt, S // 40, 2)
    lbl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    _text_center(ImageDraw.Draw(lbl), cx, cy + S * 0.09, "S C A N", _font(int(S * 0.05), "semi"), (226, 232, 240, 230))
    return Image.alpha_composite(base, lbl)


def _coin(S, x, y, R, c_light, c_dark, symbol, sym_color):
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    disk = Image.composite(Image.new("RGBA", (2 * R, 2 * R), _hex(c_dark)), Image.new("RGBA", (2 * R, 2 * R), _hex(c_light)),
                           Image.radial_gradient("L").resize((2 * R, 2 * R)))
    m = Image.new("L", (2 * R, 2 * R), 0)
    ImageDraw.Draw(m).ellipse((0, 0, 2 * R - 1, 2 * R - 1), fill=255)
    layer.paste(disk, (x - R, y - R), m)
    d = ImageDraw.Draw(layer)
    ri = int(R * 0.82)
    d.ellipse((x - ri, y - ri, x + ri, y + ri), outline=(255, 255, 255, 90), width=max(3, R // 30))
    f = _font(int(R * 1.1), "bold")
    bb = d.textbbox((0, 0), symbol, font=f)
    d.text((x - (bb[0] + bb[2]) / 2, y - (bb[1] + bb[3]) / 2), symbol, font=f, fill=_hex(sym_color))
    return layer


def _avatar_coins(S):
    cx = cy = S // 2
    base = _bg(S, "#0A1122", "#1E2F57")
    halo = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    H = int(S * 0.3)
    ImageDraw.Draw(halo).ellipse((cx - H, cy - H, cx + H, cy + H), fill=(59, 130, 246, 90))
    base = Image.alpha_composite(base, halo.filter(ImageFilter.GaussianBlur(S // 12)))

    arcs = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ad = ImageDraw.Draw(arcs)
    r, wid = int(S * 0.40), int(S * 0.02)
    _arrow_arc(ad, cx, cy, r, 200, 330, (226, 232, 240, 230), wid)
    _arrow_arc(ad, cx, cy, r, 20, 150, (226, 232, 240, 230), wid)
    base = _glow(base, arcs, S // 90, 1)

    R = int(S * 0.19)
    rub = _coin(S, int(S * 0.39), int(S * 0.45), R, "#FDE68A", "#D97706", "₽", "#3B2303")
    usd = _coin(S, int(S * 0.61), int(S * 0.55), R, "#86EFAC", "#15803D", "$", "#052E16")
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow.putalpha(usd.getchannel("A").filter(ImageFilter.GaussianBlur(S // 60)).point(lambda a: a // 2))
    base = Image.alpha_composite(base, rub)
    base = Image.alpha_composite(base, shadow.transform((S, S), Image.AFFINE, (1, 0, -S // 90, 0, 1, -S // 60)))
    base = Image.alpha_composite(base, usd)

    pill = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    f = _font(int(S * 0.055), "bold")
    w, py = f.getlength("P2P SCAN") + S * 0.06, int(S * 0.78)
    pd.rounded_rectangle((cx - w / 2, py, cx + w / 2, py + S * 0.085), radius=int(S * 0.04), fill=(15, 23, 42, 230),
                         outline=(148, 163, 184, 160), width=max(2, S // 400))
    _text_center(pd, cx, py + S * 0.008, "P2P SCAN", f, (241, 245, 249, 255))
    return Image.alpha_composite(base, pill)


AVATARS = {"radar": _avatar_radar, "neon": _avatar_neon, "coins": _avatar_coins}


def avatar(size=640, style="radar"):
    S = size * 2   # рисуем в 2x и уменьшаем — сглаживание
    return _png(AVATARS[style](S).convert("RGB").resize((size, size), Image.LANCZOS))


def avatars_preview():
    """Все варианты в круге, как их обрежет Telegram."""
    D, pad = 400, 40
    img = Image.new("RGB", (pad + len(AVATARS) * (D + pad), D + 130), BG)
    d = ImageDraw.Draw(img)
    mask = Image.new("L", (D, D), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, D - 1, D - 1), fill=255)
    for i, (name, label) in enumerate((("radar", "1. Радар"), ("neon", "2. Неон"), ("coins", "3. Монеты"))):
        x = pad + i * (D + pad)
        img.paste(Image.open(io.BytesIO(avatar(D, name))), (x, pad), mask)
        _text_center(d, x + D / 2, D + pad + 20, label, _font(34, "semi"), TEXT)
    return _png(img)


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    for style in AVATARS:
        with open(os.path.join(here, f"avatar_{style}.png"), "wb") as f:
            f.write(avatar(style=style))
    with open(os.path.join(here, "avatar.png"), "wb") as f:
        f.write(avatar(style="radar"))
    with open(os.path.join(here, "avatars_preview.png"), "wb") as f:
        f.write(avatars_preview())
    print("saved avatar_radar.png, avatar_neon.png, avatar_coins.png, avatar.png, avatars_preview.png")
