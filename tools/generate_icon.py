from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SIZE = 512


def scale(value):
    return round(value * SIZE / 256)


def box(left, top, right, bottom):
    return tuple(scale(value) for value in (left, top, right, bottom))


def main():
    ASSETS.mkdir(exist_ok=True)
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.ellipse(box(12, 12, 244, 244), fill="#123B78")
    draw.ellipse(box(22, 22, 234, 234), fill="#2563C7")
    draw.ellipse(box(33, 33, 223, 223), fill="#3B82F6")

    # The broad microphone silhouette remains clear at Windows' 16 px icon size.
    draw.rounded_rectangle(box(101, 60, 155, 151), radius=scale(27), fill="#F7FBFF")
    draw.arc(box(72, 100, 184, 190), start=0, end=180, fill="#F7FBFF", width=scale(13))
    draw.line((scale(128), scale(190), scale(128), scale(211)), fill="#F7FBFF", width=scale(13))
    draw.line((scale(96), scale(211), scale(160), scale(211)), fill="#F7FBFF", width=scale(13))
    draw.ellipse(box(179, 67, 193, 81), fill="#BFE0FF")
    draw.ellipse(box(198, 84, 208, 94), fill="#BFE0FF")
    draw.ellipse(box(211, 105, 218, 112), fill="#BFE0FF")

    png_path = ASSETS / "whisperlive.png"
    icon_path = ASSETS / "whisperlive.ico"
    image.resize((256, 256), Image.Resampling.LANCZOS).save(png_path)
    image.save(icon_path, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    main()
