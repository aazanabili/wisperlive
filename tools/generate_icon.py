from pathlib import Path

from PIL import IcoImagePlugin, Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ICON_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]


def scale(value, size):
    return round(value * size / 256)


def box(left, top, right, bottom, size):
    return tuple(scale(value, size) for value in (left, top, right, bottom))


def draw_small_icon(size):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    multiplier = size / 16
    coordinate = lambda value: round(value * multiplier)
    draw.ellipse((coordinate(0), coordinate(0), coordinate(15), coordinate(15)), fill="#2563C7")
    draw.rounded_rectangle((coordinate(6), coordinate(3), coordinate(10), coordinate(9)), radius=coordinate(2), fill="#FFFFFF")
    draw.arc((coordinate(4), coordinate(6), coordinate(12), coordinate(12)), start=0, end=180, fill="#FFFFFF", width=max(1, coordinate(1)))
    draw.line((coordinate(8), coordinate(12), coordinate(8), coordinate(13)), fill="#FFFFFF", width=max(1, coordinate(1)))
    draw.line((coordinate(5), coordinate(13), coordinate(11), coordinate(13)), fill="#FFFFFF", width=max(1, coordinate(1)))
    return image


def draw_large_icon(size):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.ellipse(box(12, 12, 244, 244, size), fill="#123B78")
    draw.ellipse(box(22, 22, 234, 234, size), fill="#2563C7")
    draw.ellipse(box(33, 33, 223, 223, size), fill="#3B82F6")

    # The broad microphone silhouette remains clear at Windows' 16 px icon size.
    draw.rounded_rectangle(box(101, 60, 155, 151, size), radius=scale(27, size), fill="#F7FBFF")
    draw.arc(box(72, 100, 184, 190, size), start=0, end=180, fill="#F7FBFF", width=scale(13, size))
    draw.line((scale(128, size), scale(190, size), scale(128, size), scale(211, size)), fill="#F7FBFF", width=scale(13, size))
    draw.line((scale(96, size), scale(211, size), scale(160, size), scale(211, size)), fill="#F7FBFF", width=scale(13, size))
    draw.ellipse(box(179, 67, 193, 81, size), fill="#BFE0FF")
    draw.ellipse(box(198, 84, 208, 94, size), fill="#BFE0FF")
    draw.ellipse(box(211, 105, 218, 112, size), fill="#BFE0FF")
    return image


def main():
    ASSETS.mkdir(exist_ok=True)
    frames = [draw_small_icon(size[0]) if size[0] <= 32 else draw_large_icon(size[0]) for size in ICON_SIZES]

    png_path = ASSETS / "whisperlive.png"
    icon_path = ASSETS / "whisperlive.ico"
    frames[-1].save(png_path)
    frames[-1].encoderinfo = {
        "append_images": frames[:-1],
        "bitmap_format": "bmp",
        "sizes": ICON_SIZES,
    }
    with open(icon_path, "wb") as file:
        IcoImagePlugin._save(frames[-1], file, str(icon_path))


if __name__ == "__main__":
    main()
