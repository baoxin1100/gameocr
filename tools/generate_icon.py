from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT / "assets"
PNG_PATH = ASSETS_DIR / "gameocr_icon.png"
ICO_PATH = ASSETS_DIR / "gameocr.ico"

ICON_LINES = ("实时", "汉化")


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("C:/Windows/Fonts/segoeuib.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
    ]
    for path in font_candidates:
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    start_size: int = 236,
    min_size: int = 96,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for size in range(start_size, min_size - 1, -4):
        font = load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width and bbox[3] - bbox[1] <= max_height:
            return font
    return load_font(min_size)


def fit_multiline_font(
    draw: ImageDraw.ImageDraw,
    lines: tuple[str, ...],
    max_width: int,
    max_height: int,
    start_size: int = 320,
    min_size: int = 120,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for size in range(start_size, min_size - 1, -4):
        font = load_font(size)
        metrics = [draw.textbbox((0, 0), line, font=font) for line in lines]
        line_gap = int(size * 0.08)
        width = max(bbox[2] - bbox[0] for bbox in metrics)
        height = sum(bbox[3] - bbox[1] for bbox in metrics) + line_gap * (len(lines) - 1)
        if width <= max_width and height <= max_height:
            return font
    return load_font(min_size)


def rounded_gradient(size: int, rect: list[int], radius: int) -> Image.Image:
    background = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(background)

    top = (116, 66, 224)
    middle = (139, 76, 236)
    bottom = (70, 32, 148)

    for y in range(size):
        t = y / (size - 1)
        if t < 0.48:
            k = t / 0.48
            color = tuple(int(top[i] * (1 - k) + middle[i] * k) for i in range(3))
        else:
            k = (t - 0.48) / 0.52
            color = tuple(int(middle[i] * (1 - k) + bottom[i] * k) for i in range(3))
        draw.line([(0, y), (size, y)], fill=(*color, 255))

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse([-180, -240, 760, 520], fill=(255, 255, 255, 78))
    glow_draw.ellipse([420, 420, 1220, 1260], fill=(205, 116, 255, 74))
    glow_draw.ellipse([520, -140, 1180, 560], fill=(152, 104, 255, 66))
    glow = glow.filter(ImageFilter.GaussianBlur(52))
    background.alpha_composite(glow)

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(rect, radius=radius, fill=255)
    background.putalpha(mask)
    return background


def create_icon() -> Image.Image:
    size = 1024
    outer_rect = [88, 112, 936, 912]
    outer_radius = 176

    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [88, 142, 936, 946],
        radius=outer_radius,
        fill=(0, 0, 0, 150),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(34))
    icon.alpha_composite(shadow)

    background = rounded_gradient(size, outer_rect, outer_radius)
    icon.alpha_composite(background)

    glass = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glass_draw = ImageDraw.Draw(glass)

    glass_draw.rounded_rectangle(
        [118, 138, 906, 896],
        radius=150,
        outline=(255, 255, 255, 84),
        width=5,
    )
    glass_draw.rounded_rectangle(
        [150, 166, 874, 480],
        radius=122,
        fill=(255, 255, 255, 58),
    )
    glass_draw.rounded_rectangle(
        [170, 198, 846, 352],
        radius=78,
        fill=(255, 255, 255, 38),
    )
    glass_draw.rounded_rectangle(
        [158, 600, 866, 870],
        radius=112,
        fill=(32, 12, 82, 56),
    )
    glass = glass.filter(ImageFilter.GaussianBlur(1.2))
    icon.alpha_composite(glass)

    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle(
        outer_rect,
        radius=outer_radius,
        outline=(255, 255, 255, 112),
        width=8,
    )
    draw.rounded_rectangle(
        [104, 128, 920, 896],
        radius=160,
        outline=(255, 255, 255, 42),
        width=3,
    )

    font = fit_multiline_font(draw, ICON_LINES, max_width=650, max_height=560, start_size=304)
    line_bboxes = [draw.textbbox((0, 0), line, font=font) for line in ICON_LINES]
    line_gap = int(getattr(font, "size", 220) * 0.08)
    line_heights = [bbox[3] - bbox[1] for bbox in line_bboxes]
    total_height = sum(line_heights) + line_gap * (len(ICON_LINES) - 1)
    current_y = (size - total_height) // 2 - 8

    text_positions: list[tuple[str, int, int]] = []
    for line, bbox, line_height in zip(ICON_LINES, line_bboxes, line_heights):
        text_width = bbox[2] - bbox[0]
        text_x = (size - text_width) // 2 - bbox[0]
        text_y = current_y - bbox[1]
        text_positions.append((line, text_x, text_y))
        current_y += line_height + line_gap

    # Subtle glow/shadow only; the icon content itself remains the requested
    # four white characters, arranged as "实时" above "汉化".
    glow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    for line, text_x, text_y in text_positions:
        glow_draw.text((text_x, text_y), line, font=font, fill=(255, 255, 255, 160))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(12))
    icon.alpha_composite(glow_layer)

    draw = ImageDraw.Draw(icon)
    for line, text_x, text_y in text_positions:
        draw.text((text_x + 5, text_y + 7), line, font=font, fill=(32, 10, 72, 122))
        draw.text((text_x, text_y), line, font=font, fill=(255, 255, 255, 255))

    return icon


def main() -> None:
    ASSETS_DIR.mkdir(exist_ok=True)
    icon = create_icon()
    icon.save(PNG_PATH)
    icon.save(
        ICO_PATH,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    for path in (PNG_PATH, ICO_PATH):
        with Image.open(path) as image:
            print(f"{path.relative_to(ROOT)} {image.size} {image.mode}")


if __name__ == "__main__":
    main()