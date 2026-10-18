from io import BytesIO

from PIL import Image

from vk_tg_bridge.avatar import AVATAR_SIZE, animated_sticker_gif, badge_avatar, sticker_on_white


def test_badge_makes_a_square_jpeg_with_a_blue_corner() -> None:
    source = Image.new("RGB", (80, 40), (240, 200, 180))
    raw = BytesIO()
    source.save(raw, format="PNG")
    result = badge_avatar(raw.getvalue())
    assert result is not None
    image = Image.open(BytesIO(result))
    assert image.size == (AVATAR_SIZE, AVATAR_SIZE)
    assert image.format == "JPEG"
    corner = image.getpixel((AVATAR_SIZE - 70, AVATAR_SIZE - 70))
    red, _green, blue = corner[:3]
    assert blue > red
    assert blue > 80


def test_badge_rejects_a_non_image() -> None:
    assert badge_avatar(b"not-an-image") is None


def test_sticker_keeps_its_shape_on_a_white_background() -> None:
    source = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    for x in range(20):
        for y in range(20):
            source.putpixel((x, y), (255, 0, 0, 255))
    raw = BytesIO()
    source.save(raw, format="PNG")
    result = sticker_on_white(raw.getvalue())
    assert result is not None
    image = Image.open(BytesIO(result))
    assert image.format == "JPEG"
    assert image.size == (40, 20)
    red = image.getpixel((8, 8))
    white = image.getpixel((32, 8))
    assert red[0] > 200
    assert red[1] < 40
    assert white[0] > 240 and white[1] > 240 and white[2] > 240


def test_animated_webp_sticker_becomes_a_gif() -> None:
    frames = [
        Image.new("RGBA", (8, 8), (255, 0, 0, 255)),
        Image.new("RGBA", (8, 8), (0, 0, 255, 0)),
    ]
    raw = BytesIO()
    frames[0].save(
        raw,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=80,
        loop=0,
    )
    result = animated_sticker_gif(raw.getvalue())
    assert result is not None
    image = Image.open(BytesIO(result))
    assert image.format == "GIF"
    assert image.n_frames == 2
    assert animated_sticker_gif(b"not-an-image") is None
