from io import BytesIO

from PIL import Image

from vk_tg_bridge.avatar import AVATAR_SIZE, badge_avatar


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
