"""Legacy thumbnail normalization of already-authorized image bytes; no fetching."""
import io
from PIL import Image

MAX_IMAGE_PIXELS = 16_000_000


def thumbnail_bytes(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > 5 * 1024 * 1024:
        raise ValueError("Image bytes must contain at most 5 MiB")
    # Inspect dimensions before decoding/allocating the full pixel buffer.
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError("Image must contain at most 16 million pixels")
            img = source.convert("RGB")
    except Image.DecompressionBombError as exc:
        raise ValueError("Image dimensions exceed the supported limit") from exc
    img.thumbnail((256, 256))
    canvas = Image.new("RGB", (256, 256), (255, 255, 255))
    x = (256 - img.width) // 2
    y = (256 - img.height) // 2
    canvas.paste(img, (x, y))
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()
