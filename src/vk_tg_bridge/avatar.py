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


def sticker_on_white(content: bytes) -> bytes | None:
    try:
        image = Image.open(BytesIO(content)).convert("RGBA")
    except (OSError, ValueError):
        return None
    canvas = Image.new("RGB", image.size, (255, 255, 255))
    canvas.paste(image, mask=image.getchannel("A"))
    output = BytesIO()
    canvas.save(output, format="JPEG", quality=90)
    return output.getvalue()


def animated_sticker_gif(content: bytes) -> bytes | None:
    try:
        image = Image.open(BytesIO(content))
    except (OSError, ValueError):
        return None
    frames = getattr(image, "n_frames", 1)
    if not isinstance(frames, int) or frames <= 1:
        return None
    pictures: list[Image.Image] = []
    durations: list[int] = []
    try:
        for index in range(frames):
            image.seek(index)
            pictures.append(_gif_frame(image.convert("RGBA")))
            duration = image.info.get("duration")
            durations.append(int(duration) if isinstance(duration, int) and duration > 0 else 100)
    except (OSError, ValueError):
        return None
    output = BytesIO()
    pictures[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=pictures[1:],
        duration=durations,
        loop=0,
        disposal=2,
        transparency=255,
    )
    return output.getvalue()


def _gif_frame(frame: Image.Image) -> Image.Image:
    alpha = frame.getchannel("A")
    palette = frame.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE, colors=255)
    transparent = alpha.point(lambda value: 255 if value < 128 else 0)
    palette.paste(255, mask=transparent)
    return palette


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
