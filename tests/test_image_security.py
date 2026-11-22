import io
import unittest
from unittest.mock import patch

from PIL import Image
from aiqs_si.profile_image import thumbnail_bytes


class ImageSecurityTests(unittest.TestCase):
    def png(self, size):
        out = io.BytesIO()
        Image.new("RGB", size).save(out, format="PNG")
        return out.getvalue()

    def test_rejects_compressed_image_before_pixel_conversion(self):
        raw = self.png((11, 10))
        with patch("aiqs_si.profile_image.MAX_IMAGE_PIXELS", 100):
            with patch.object(Image.Image, "convert", side_effect=AssertionError("decoded too early")):
                with self.assertRaisesRegex(ValueError, "million pixels"):
                    thumbnail_bytes(raw)

    def test_accepts_exact_pixel_boundary(self):
        raw = self.png((10, 10))
        with patch("aiqs_si.profile_image.MAX_IMAGE_PIXELS", 100):
            with Image.open(io.BytesIO(thumbnail_bytes(raw))) as result:
                self.assertEqual(result.size, (256, 256))
                self.assertEqual(result.format, "JPEG")

    def test_normalizes_pillow_bomb_error(self):
        with patch("aiqs_si.profile_image.Image.open", side_effect=Image.DecompressionBombError):
            with self.assertRaises(ValueError):
                thumbnail_bytes(b"synthetic")
