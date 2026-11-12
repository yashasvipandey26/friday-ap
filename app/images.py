"""Gets a photographed invoice ready for the model. Only the copy sent to the model changes; the stored
original is kept as received.

Phone photos cause three quiet failures if sent as they are: the picture is stored sideways with an
"orientation" tag the model may ignore, it is often bigger than the AI service accepts (Claude takes
up to 5 MB per image), and iPhones save HEIC, which neither service reads everywhere. Each is fixed here.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

try:  # HEIC/HEIF, the iPhone camera format
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - the app still reads JPEG, PNG and WebP without it
    pillow_heif = None

MAX_EDGE = 2400  # pixels on the long side: plenty for small print, and well inside every model's limits
MAX_BYTES = 3_500_000  # stay comfortably under Claude's 5 MB per image after base64
Image.MAX_IMAGE_PIXELS = 60_000_000  # a "picture" bigger than this is a decompression bomb, not an invoice


class ImageError(Exception):
    """The image can't be opened. The message is shown to the user as-is."""


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    media_type: str
    changes: list[str]  # what was done, in words, for the run view


def prepare_image(data: bytes, media_type: str) -> PreparedImage:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise ImageError("The image is damaged or isn't a picture format we can open.") from error

    changes = []
    upright = ImageOps.exif_transpose(image)
    if upright.size != image.size or _orientation(image) not in (None, 1):
        changes.append("turned upright")
    image = upright

    if max(image.size) > MAX_EDGE:
        image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
        changes.append(f"scaled to {image.width} x {image.height} px")

    must_convert = media_type not in ("image/jpeg", "image/png", "image/webp")
    if must_convert:
        changes.append("converted from HEIC to JPEG" if "hei" in media_type else "converted to JPEG")
    if not changes and len(data) <= MAX_BYTES:
        return PreparedImage(data, media_type, [])  # already fine: send it exactly as received

    for quality in (88, 80, 70):
        output = io.BytesIO()
        image.convert("RGB").save(output, format="JPEG", quality=quality, optimize=True)
        if output.tell() <= MAX_BYTES:
            break
    if "scaled" not in " ".join(changes) and len(data) > MAX_BYTES:
        changes.append("compressed")
    return PreparedImage(output.getvalue(), "image/jpeg", changes)


def _orientation(image: Image.Image) -> int | None:
    try:
        return image.getexif().get(0x0112)
    except Exception:  # a broken EXIF block shouldn't stop the invoice
        return None
