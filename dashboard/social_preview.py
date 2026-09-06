"""Render the social card without database, network, or application startup work."""

import io
import math
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont

CREAM = "#f3ead3"
GOLD = "#dfc187"
MUTED = "#bcb8cb"


def _value(row, key):
    value = row.get(key.upper())
    return row.get(key.lower()) if value is None else value


def format_number(value, digits=1, suffix=""):
    """Format display values without losing zero or exposing float precision."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "—"
    if not math.isfinite(number):
        return "—"
    return f"{number:,.{digits}f}{suffix}"


def _font(static_dir, size, heavy=True):
    name = "nunito_900.ttf" if heavy else "nunito_800.ttf"
    try:
        return ImageFont.truetype(str(Path(static_dir) / name), size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def fit_text(draw, text, box, static_dir, max_size, min_size=16, heavy=True):
    """Shrink, then truncate text so its measured ink fits the supplied box."""
    text = " ".join(str(text).split())
    width, height = box[2] - box[0], box[3] - box[1]
    for size in range(max_size, min_size - 1, -1):
        font = _font(static_dir, size, heavy)
        bounds = draw.textbbox((0, 0), text, font=font)
        if bounds[2] - bounds[0] <= width and bounds[3] - bounds[1] <= height:
            return text, font, bounds
    while text:
        shortened = text.rstrip() + "…"
        bounds = draw.textbbox((0, 0), shortened, font=font)
        if bounds[2] - bounds[0] <= width and bounds[3] - bounds[1] <= height:
            return shortened, font, bounds
        text = text[:-1]
    return "", font, (0, 0, 0, 0)


def _text(draw, text, box, static_dir, size, color=CREAM, align="left", heavy=True):
    text, font, bounds = fit_text(draw, text, box, static_dir, size, heavy=heavy)
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    x = box[0] + ((box[2] - box[0] - width) / 2 if align == "center" else 0)
    y = box[1] + (box[3] - box[1] - height) / 2
    draw.text((x - bounds[0], y - bounds[1]), text, font=font, fill=color)


def _ball(draw, x, y, radius, color=GOLD, fill="#23263f"):
    box = (x-radius, y-radius, x+radius, y+radius)
    draw.ellipse(box, fill=fill, outline=color, width=2)
    draw.pieslice(box, 180, 360, fill=color)
    draw.line((x-radius, y, x+radius, y), fill=color, width=3)
    r = max(5, int(radius * .28))
    draw.ellipse((x-r, y-r, x+r, y+r), fill=fill, outline=color, width=3)


def _crest(draw, x, y):
    _ball(draw, x, y, 35)
    draw.polygon([(x-30, y-46), (x-38, y-68), (x-13, y-59),
                  (x, y-78), (x+13, y-59), (x+38, y-68), (x+30, y-46)], fill=GOLD)
    for side in (-1, 1):
        draw.arc((x-61, y-54, x+61, y+60), 90 if side < 0 else 0,
                 180 if side < 0 else 90, fill=GOLD, width=2)
        for dy, dx in [(-27, 58), (-7, 63), (15, 58), (35, 45)]:
            px = x + side * dx
            draw.polygon([(px, y+dy+10), (px+side*14, y+dy-3),
                          (px+side*9, y+dy-16), (px-side*3, y+dy-3)], fill=GOLD)


def _world_honor(champion, record):
    if not isinstance(record, dict):
        return None
    name = " ".join(str(_value(champion, "player_name") or "").split()).casefold()
    holder = " ".join(str(record.get("playerName") or "").split()).casefold()
    try:
        source = urlparse(str(record.get("sourceUrl") or ""))
    except ValueError:
        return None
    year = record.get("year")
    if (name and name == holder and type(year) is int
            and source.scheme == "https" and source.hostname in ("pokemon.com", "www.pokemon.com")):
        return f"{year} world champion · {record.get('division', 'TCG Masters')}"
    return None


def render_social_preview(champion, static_dir, world_champion=None):
    """Return a 1200×630 PNG. Championship honors require an explicit matching record."""
    image = Image.new("RGB", (1200, 630), "#192133")
    draw = ImageDraw.Draw(image)
    for y in range(630):
        t = y / 629
        draw.line((0, y, 1199, y), fill=(int(38-17*t), int(42-12*t), int(66-18*t)))
    draw.ellipse((1040, 18, 1168, 146), fill="#5c5d73")
    draw.ellipse((1026, 6, 1142, 129), fill="#282d46")
    for x, top, scale in [(25, 310, 1.1), (1160, 245, 1.5), (1120, 350, .85), (1210, 130, 1.3)]:
        for level in range(4):
            y = top + level * 44 * scale
            spread = (22 + level * 14) * scale
            draw.polygon([(x, y-65*scale), (x-spread, y+24*scale),
                          (x+spread, y+24*scale)], fill="#343951")
        draw.line((x, top, x, 630), fill="#343951", width=5)

    _ball(draw, 72, 71, 24, CREAM, "#263047")
    _text(draw, "indigo circuit", (116, 39, 850, 91), static_dir, 48)
    _text(draw, "the competitive pokémon field guide", (50, 105, 1000, 137), static_dir, 25, MUTED, heavy=False)

    draw.rounded_rectangle((48, 166, 1152, 558), radius=20, fill="#20243b", outline="#a58c61", width=2)
    draw.rounded_rectangle((57, 175, 1143, 549), radius=13, outline="#454151", width=1)
    _crest(draw, 1039, 283)
    row = champion or {}
    honor = _world_honor(row, world_champion)
    title = "league champion" if champion else "the league"
    _text(draw, title, (83, 192, 452, 229), static_dir, 26, GOLD)
    if honor:
        _text(draw, honor, (465, 192, 1108, 229), static_dir, 23, GOLD, heavy=False)
    name = _value(row, "player_name") or "the league awaits."
    _text(draw, name, (80, 242, 932, 331), static_dir, 78)
    deck = _value(row, "top_deck_name")
    subtitle = f"season archetype · {deck}" if deck else "a rolling 52-week view of competitive play"
    _text(draw, subtitle, (83, 344, 936, 380), static_dir, 26, MUTED, heavy=False)

    draw.line((84, 405, 1116, 405), fill="#5b5060", width=1)
    stats = [(format_number(_value(row, "atp_score")), "league points"),
             (format_number(_value(row, "win_rate_pct"), suffix="%"), "win rate"),
             (format_number(_value(row, "top8s"), digits=0), "top 8 finishes")]
    for index, (value, label) in enumerate(stats):
        left = 84 + index * 344
        if index:
            draw.line((left-16, 426, left-16, 522), fill="#454151", width=1)
        _text(draw, value, (left, 425, left+305, 485), static_dir, 52, GOLD)
        _text(draw, label, (left, 498, left+305, 529), static_dir, 23, MUTED, heavy=False)

    _text(draw, "read the field. find your edge.", (50, 582, 805, 612), static_dir, 23, MUTED, heavy=False)
    _text(draw, "indigocircuit.app", (880, 582, 1150, 612), static_dir, 23, CREAM)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
