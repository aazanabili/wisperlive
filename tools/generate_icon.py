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

    draw.ellipse(box(12, 12, 244, 244), fill="#173C78")
    draw.ellipse(box(20, 20, 236, 236), fill="#2868C7")
    draw.ellipse(box(31, 31, 225, 225), fill="#3B82F6")

    # A microphone at the center and two speech-wave arcs keep the mark legible at small sizes.
    draw.rounded_rectangle(box(103, 62, 153, 150), radius=scale(25), fill="#F7FBFF")
    draw.arc(box(75, 101, 181, 188), start=0, end=180, fill="#F7FBFF", width=scale(12))
    draw.line((scale(128), scale(188), scale(128), scale(209)), fill="#F7FBFF", width=scale(12))
    draw.line((scale(99), scale(209), scale(157), scale(209)), fill="#F7FBFF", width=scale(12))
    draw.arc(box(38, 74, 218, 181), start=302, end=58, fill="#BFE0FF", width=scale(10))
    draw.arc(box(53, 86, 203, 169), start=304, end=56, fill="#BFE0FF", width=scale(8))

    png_path = ASSETS / "whisperlive.png"
    icon_path = ASSETS / "whisperlive.ico"
    image.resize((256, 256), Image.Resampling.LANCZOS).save(png_path)
    image.save(icon_path, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    main()
