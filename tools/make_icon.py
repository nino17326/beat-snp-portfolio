# ============================================================
#  tools/make_icon.py  -  draws assets/portfolio.ico (a simple rising
#  line chart on a rounded tile). Run it again only to change the icon.
# ============================================================

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets" / "portfolio.ico"
SIZE = 256


def draw():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], radius=48, fill=(27, 94, 168, 255))      # blue tile
    for y in (88, 136, 184):                                                                 # faint grid
        d.line([(40, y), (216, y)], fill=(255, 255, 255, 60), width=4)
    points = [(44, 196), (92, 150), (132, 168), (176, 104), (212, 64)]                        # rising line
    d.line(points, fill=(255, 255, 255, 255), width=18, joint="curve")
    for x, y in points:
        d.ellipse([x - 12, y - 12, x + 12, y + 12], fill=(255, 255, 255, 255))
    return img


if __name__ == "__main__":
    OUT.parent.mkdir(exist_ok=True)
    draw().save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Saved {OUT}")
