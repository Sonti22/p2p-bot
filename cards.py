"""Картинки для бота: карточка связки, график топа, аватарка. Pillow + шрифты Windows.

Аватарка в файл:  python cards.py
"""
import io
import math
import os
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from p2p import _money, _price, route_actions, sell_step_number, terms_flags

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


def _plain(text):
    """Текст без эмодзи и служебных символов: в шрифте карточки (Segoe UI) их нет, Pillow рисует квадраты."""
    out = "".join(ch for ch in str(text) if not (ord(ch) >= 0x1F000 or 0x2600 <= ord(ch) <= 0x27BF
                                                 or ord(ch) in (0xFE0F, 0x200D, 0x20E3)))
    return " ".join(out.split())


def _step_box(d, x, y, w, h, num, title, color, ad, pays_title):
    """Шаг «купить»/«продать»: номер и действие, площадка, цена, кто, как платить, условия мерчанта."""
    d.rounded_rectangle((x, y, x + w, y + h), radius=22, fill=PANEL, outline=color, width=3)
    f_pill, f_mid, f_small = _font(22, "bold"), _font(24, "semi"), _font(21)
    nx = _pill(d, x + 18, y + 18, f"{num}  {title}", color, f_pill)
    _pill(d, nx + 10, y + 18, ad.ex, VENUE_COLORS.get(ad.ex, BLUE), f_pill)
    price, per = f"{_price(ad.price)} ₽", f"за 1 {ad.asset}"
    size = 44   # цена BTC в рублях длинная — уменьшаем шрифт, пока цена с «за 1 BTC» не влезет в рамку
    while size > 28 and _font(size, "bold").getlength(price) + 8 + f_small.getlength(per) > w - 40:
        size -= 2
    f_price = _font(size, "bold")
    d.text((x + 20, y + 70 + (44 - size) // 2), price, font=f_price, fill=TEXT)
    d.text((x + 28 + f_price.getlength(price), y + 90), per, font=f_small, fill=MUTED)
    who = "Обменник" if ad.ex == "BestChange" else ("Продавец" if title == "КУПИТЬ" else "Покупатель")
    d.text((x + 20, y + 132), _fit(f"{who}: {_plain(ad.nick)}", f_mid, w - 40), font=f_mid, fill=TEXT)
    stats = f"{ad.orders} отзывов · {ad.rate:.0f}% хороших" if ad.ex == "BestChange" else f"{ad.orders} сделок · {ad.rate:.0f}% успешных"
    d.text((x + 20, y + 166), stats, font=f_small, fill=MUTED)
    d.text((x + 20, y + 198), _fit(f"{pays_title}: {_plain(', '.join(ad.pays))}", f_small, w - 40), font=f_small, fill=TEXT)
    notes = terms_flags(ad.terms)[1]
    if notes:   # условия мерчанта словами, до двух строк
        ty = y + 232
        for ln in _wrap("Условия: " + _plain("; ".join(notes)), _font(20), w - 40)[:2]:
            d.text((x + 20, ty), ln, font=_font(20), fill=AMBER)
            ty += 25


REL_COLORS = {"✅ надёжно": GREEN, "⚠️ риск": AMBER, "🪤 ловушка": RED}


def _amounts_chips(d, x, y, amounts, W):
    """«Другая сумма круга»: плашки «10 000 ₽ +3.95%», серые — если на эту сумму не хватает глубины."""
    f_lbl, f_chip = _font(22), _font(22, "semi")
    d.text((x, y + 6), "На другую сумму:", font=f_lbl, fill=MUTED)
    cx = x + f_lbl.getlength("На другую сумму:") + 16
    for amt, val in amounts.items():
        text = f"{_money(amt)} ₽  {val:+.2f}%" if val is not None else f"{_money(amt)} ₽  нет объёма"
        w = f_chip.getlength(text) + 28
        if cx + w > W - 40:
            break
        d.rounded_rectangle((cx, y, cx + w, y + 38), radius=19, fill=PANEL, outline=BORDER, width=2)
        d.text((cx + 14, y + 6), text, font=f_chip, fill=(_profit_color(val) if val is not None else MUTED))
        cx += w + 10


def deal_card(deal, cfg, amounts=None, rel=None):
    """Карточка сигнала — те же шаги, что в подписи и на кнопках: 1 купить → 2… перевод/спот → N продать.
    Без эмодзи (в шрифте их нет) и без мелкого текста: всё читается с телефона. Прибыль по стадиям —
    в «📝 Инструкция» (steps_view), на картинке только итог."""
    profit, b, s, route = deal
    acts, _costs = route_actions(route)
    W, H = 1080, 720 if amounts else 660
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((40, 28), "СИГНАЛ · P2P-СВЯЗКА", font=_font(24, "semi"), fill=MUTED)
    stamp = time.strftime("%d.%m %H:%M")
    d.text((W - 40 - _font(24).getlength(stamp), 28), stamp, font=_font(24), fill=MUTED)

    color = AMBER if profit >= 5 else GREEN if profit > 0 else RED
    big = f"{profit:+.2f}%"
    f_big = _font(96, "bold")
    d.text((36, 56), big, font=f_big, fill=color)
    d.text((56 + f_big.getlength(big), 106), f"чистыми на {_money(cfg.amount)} ₽", font=_font(32), fill=TEXT)
    d.text((40, 176), f"{b.ex} {b.asset}  →  {s.ex} {s.asset}", font=_font(30, "semi"), fill=TEXT)
    if rel:
        label = _plain(rel[0]).upper()
        if len(rel) > 2:   # индекс надёжности 0–10 рядом с меткой
            label = f"{label} · {rel[2]}/10"
        f_rel = _font(24, "bold")
        pw = f_rel.getlength(label) + 24
        _pill(d, W - 40 - pw, 172, label, REL_COLORS.get(rel[0], AMBER), f_rel)

    y, h, bw = 236, 290, 350
    sell_n = sell_step_number(route)
    _step_box(d, 40, y, bw, h, 1, "КУПИТЬ", GREEN, b, "Оплата")
    _step_box(d, W - 40 - bw, y, bw, h, sell_n, "ПРОДАТЬ", RED, s, "Получить на")
    # середина: шаги между покупкой и продажей (перевод, спот) с номерами, как в подписи
    mx0, mx1 = 40 + bw + 20, W - 40 - bw - 20
    mid, colw = (mx0 + mx1) / 2, mx1 - mx0
    f_step, f_num = _font(20), _font(22, "bold")
    lines = []
    for i, st in enumerate(acts[:5], 2):
        st = _plain(st)
        wrapped = _wrap(st[:1].upper() + st[1:], f_step, colw - 10)[:3]
        lines.append((f"{i}", wrapped))
    if not lines:
        lines.append(("", ["без перевода:", "всё на одной площадке"]))
    rows = sum(1 + len(w) for _, w in lines)
    ty = y + max(10, (h - rows * 26 - 40) / 2)
    for num, wrapped in lines:
        if num:
            r = 16
            d.ellipse((mid - r, ty, mid + r, ty + 2 * r), fill=BLUE)
            d.text((mid - f_num.getlength(num) / 2, ty + 2), num, font=f_num, fill="#0B0F14")
            ty += 2 * r + 4
        for ln in wrapped:
            d.text((mid - f_step.getlength(ln) / 2, ty), ln, font=f_step, fill=TEXT if num else MUTED)
            ty += 25
        ty += 6
    ay = min(ty + 14, y + h - 14)
    d.line((mid - 90, ay, mid + 80, ay), fill=MUTED, width=4)
    d.polygon([(mid + 94, ay), (mid + 76, ay - 12), (mid + 76, ay + 12)], fill=MUTED)

    fy = y + h + 22
    if amounts:
        _amounts_chips(d, 40, fy, amounts, W)
        fy += 60
    d.rounded_rectangle((40, fy, W - 40, fy + 76), radius=18, fill="#2A2112", outline="#5C4513", width=2)
    d.ellipse((62, fy + 23, 92, fy + 53), fill=AMBER)
    d.text((72, fy + 22), "!", font=_font(24, "bold"), fill="#0B0F14")
    note = "Проверь ФИО отправителя и условия мерчанта. Первая сделка — малой суммой."
    if profit >= 5:
        note = "Спред от 5% часто плата за риск: проверь мерчанта и ФИО, начни с малой суммы."
    if rel and rel[1]:
        note = _plain(rel[1][0][0].upper() + rel[1][0][1:]) + ". Проверь мерчанта, начни с малой суммы."
    d.text((108, fy + 22), _fit(note, _font(23), W - 170), font=_font(23), fill="#F5D9A8")
    return _png(img)


def portfolio_card(rows, total):
    """Карточка баланса: rows — [(биржа, [(монета, кол-во, ₽ или None)])], total — итог в ₽."""
    coin_lines = sum(len(coins) for _, coins in rows) or 1
    W = 1080
    H = 190 + len(rows) * 46 + coin_lines * 40 + 110
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((40, 30), "💰 Баланс по биржам", font=_font(40, "bold"), fill=TEXT)
    stamp = time.strftime("%d.%m %H:%M")
    d.text((W - 40 - _font(22).getlength(stamp), 46), stamp, font=_font(22), fill=MUTED)
    d.line((40, 96, W - 40, 96), fill=BORDER, width=2)

    y = 122
    f_ex, f_coin, f_val = _font(28, "semi"), _font(24), _font(24)
    for name, coins in rows:
        d.text((40, y), name, font=f_ex, fill=VENUE_COLORS.get(name, BLUE))
        y += 44
        for coin, amt, rub in coins:
            d.text((64, y), f"{amt:g} {coin}", font=f_coin, fill=TEXT)
            val = f"≈ {_money(rub)} ₽" if rub else "нет ориентира"
            d.text((W - 40 - f_val.getlength(val), y), val, font=f_val, fill=TEXT if rub else MUTED)
            y += 40
        y += 6

    d.rounded_rectangle((40, H - 90, W - 40, H - 30), radius=18, fill=PANEL, outline=BORDER, width=2)
    tot = f"Итого: ≈ {_money(total)} ₽"
    d.text((64, H - 74), tot, font=_font(32, "bold"), fill=GREEN)
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


def _rgb(hexc):
    hexc = hexc.lstrip("#")
    return tuple(int(hexc[i:i + 2], 16) for i in (0, 2, 4))


def _lerp_rgb(c1, c2, t):
    return tuple(round(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _heat_color(v, vmax):
    """Цвет ячейки хитмапа: нет данных — панель, дальше градиент BORDER → GREEN → AMBER."""
    if v is None:
        return PANEL
    t = max(0.0, min(1.0, v / vmax)) if vmax > 0 else 0.0
    c1, c2, c3 = _rgb(BORDER), _rgb(GREEN), _rgb(AMBER)
    rgb = _lerp_rgb(c1, c2, t * 2) if t < 0.5 else _lerp_rgb(c2, c3, (t - 0.5) * 2)
    return "#%02X%02X%02X" % rgb


def _profit_color(v):
    return AMBER if v >= 5 else GREEN if v >= 2 else BLUE if v > 0 else RED


HOUR_LABELS = tuple(range(0, 24, 3))
DOW_NAMES = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def history_card(hourly, grid):
    """«Лучшее время суток» (средний % по часам МСК, 7 дней) + хитмап час × день недели."""
    W, H = 1080, 720
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((40, 30), "📈 Лучшее время суток (МСК, 7 дней)", font=_font(34, "bold"), fill=TEXT)
    known = {h: v for h, v in hourly.items() if v is not None}
    if known:
        best_h = max(known, key=known.get)
        worst_h = min(known, key=known.get)
        d.text((40, 74), f"Лучший час: {best_h:02d}:00 ({known[best_h]:+.2f}%) · "
                         f"худший: {worst_h:02d}:00 ({known[worst_h]:+.2f}%)", font=_font(22), fill=MUTED)
    else:
        d.text((40, 74), "Данных пока мало — история копится раз в 5 минут.", font=_font(22), fill=MUTED)

    x0, x1, y_top, y_bot = 60, 1040, 140, 340
    vals = [v for v in hourly.values() if v is not None]
    vmin, vmax = min(vals + [0.0]), max(vals + [0.1])
    if vmax == vmin:
        vmax = vmin + 0.1
    zero_y = y_bot - (0 - vmin) / (vmax - vmin) * (y_bot - y_top)
    d.line((x0, zero_y, x1, zero_y), fill=BORDER, width=2)
    gap, bw = 4, (x1 - x0 - 23 * 4) / 24
    for h in range(24):
        x = x0 + h * (bw + gap)
        v = hourly.get(h)
        if v is None:
            d.ellipse((x + bw / 2 - 3, zero_y - 3, x + bw / 2 + 3, zero_y + 3), fill=BORDER)
        else:
            y = y_bot - (v - vmin) / (vmax - vmin) * (y_bot - y_top)
            top, bot = min(y, zero_y), max(y, zero_y)
            d.rectangle((x, top, x + bw, max(bot, top + 2)), fill=_profit_color(v))
        if h in HOUR_LABELS:
            d.text((x, y_bot + 8), f"{h:02d}", font=_font(16), fill=MUTED)

    gy0 = 390
    d.text((40, gy0), "Хитмап: лучший % по часам и дням недели (7 дней)", font=_font(26, "semi"), fill=TEXT)
    grid_x0, grid_y0, cell_h = 112, gy0 + 40, 26
    cw = (x1 - grid_x0) / 24
    gvals = [v for v in grid.values()]
    gvmax = max(gvals + [2.0])
    for dow in range(7):
        ry = grid_y0 + dow * cell_h
        d.text((40, ry + 4), DOW_NAMES[dow], font=_font(18, "semi"), fill=MUTED)
        for h in range(24):
            v = grid.get((dow, h))
            x = grid_x0 + h * cw
            d.rectangle((x, ry, x + cw, ry + cell_h - 2), fill=_heat_color(v, gvmax))
    hy = grid_y0 + 7 * cell_h + 6
    for h in HOUR_LABELS:
        d.text((grid_x0 + h * cw, hy), f"{h:02d}", font=_font(16), fill=MUTED)

    ly = H - 60
    for x, color, label in ((40, BORDER, "нет данных"), (280, GREEN, "прибыльно"), (500, AMBER, "лучшие часы")):
        d.rounded_rectangle((x, ly + 4, x + 22, ly + 24), radius=5, fill=color)
        d.text((x + 32, ly), label, font=_font(20), fill=MUTED)
    return _png(img)


def history_compare_card(labels, p2p, bc):
    """Медиана лучшего % по дням: связки P2P против связок через BestChange."""
    W, H = 1080, 480
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((40, 30), "📉 P2P против BestChange", font=_font(34, "bold"), fill=TEXT)
    if not labels:
        d.text((40, 90), "История пока пуста — данные появятся после первых сканов.", font=_font(26), fill=MUTED)
        return _png(img)
    d.text((40, 74), f"Медиана лучшего % по дням, {len(labels)} дн.", font=_font(22), fill=MUTED)

    x0, x1, y_top, y_bot = 70, 1040, 160, 400
    vals = [v for v in p2p + bc if v is not None]
    vmin, vmax = min(vals + [0.0]), max(vals + [0.1])
    if vmax == vmin:
        vmax = vmin + 0.1
    pad = (vmax - vmin) * 0.1
    vmin, vmax = vmin - pad, vmax + pad

    def y_of(v):
        return y_bot - (v - vmin) / (vmax - vmin) * (y_bot - y_top)

    if vmin < 0 < vmax:
        d.line((x0, y_of(0), x1, y_of(0)), fill=BORDER, width=2)
    n = len(labels)
    step = (x1 - x0) / max(n - 1, 1)
    label_step = max(1, n // 10)

    def draw_series(vals, color, name):
        pts = [(x0 + i * step, y_of(v)) if v is not None else None for i, v in enumerate(vals)]
        for i in range(len(pts) - 1):
            if pts[i] and pts[i + 1]:
                d.line((*pts[i], *pts[i + 1]), fill=color, width=4)
        for p in pts:
            if p:
                d.ellipse((p[0] - 5, p[1] - 5, p[0] + 5, p[1] + 5), fill=color)
        last = next((v for v in reversed(vals) if v is not None), None)
        return f"{name} {last:+.2f}%" if last is not None else f"{name} —"

    p2p_lbl = draw_series(p2p, GREEN, "P2P")
    bc_lbl = draw_series(bc, BLUE, "BestChange")
    for i, lbl in enumerate(labels):
        if i % label_step == 0:
            d.text((x0 + i * step - 16, y_bot + 10), lbl, font=_font(16), fill=MUTED)

    ly = H - 40
    for x, color, label in ((40, GREEN, p2p_lbl), (280, BLUE, bc_lbl)):
        d.rounded_rectangle((x, ly + 2, x + 22, ly + 22), radius=5, fill=color)
        d.text((x + 32, ly - 2), label, font=_font(22, "semi"), fill=TEXT)
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
