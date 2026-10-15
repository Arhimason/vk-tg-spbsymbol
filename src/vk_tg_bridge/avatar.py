import logging
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

AVATAR_SIZE = 512
BADGE_MARGIN = 28
BADGE_SIZE = 148
VK_BLUE = (0, 119, 255, 255)
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def badge_avatar(content: bytes) -> bytes | None:
    try:
        source = Image.open(BytesIO(content))
        image = _square(source.convert("RGBA")).resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        logger.error("аватар VK не открылся как картинка")
        return None
    _draw_badge(image)
    output = BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=90)
    return output.getvalue()


def _square(image: Image.Image) -> Image.Image:
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    return image.crop((left, top, left + side, top + side))


def _draw_badge(image: Image.Image) -> None:
    draw = ImageDraw.Draw(image)
    left = AVATAR_SIZE - BADGE_MARGIN - BADGE_SIZE
    top = AVATAR_SIZE - BADGE_MARGIN - BADGE_SIZE
    ring = 8
    draw.rounded_rectangle(
        (left - ring, top - ring, left + BADGE_SIZE + ring, top + BADGE_SIZE + ring),
        radius=36,
        fill=(255, 255, 255, 255),
    )
    draw.rounded_rectangle(
        (left, top, left + BADGE_SIZE, top + BADGE_SIZE),
        radius=28,
        fill=VK_BLUE,
    )
    font = _font(size=72)
    text = "VK"
    box = draw.textbbox((0, 0), text, font=font)
    width = box[2] - box[0]
    height = box[3] - box[1]
    draw.text(
        (left + (BADGE_SIZE - width) / 2, top + (BADGE_SIZE - height) / 2 - box[1]),
        text,
        fill=(255, 255, 255, 255),
        font=font,
    )


def _font(*, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default(size=size)
