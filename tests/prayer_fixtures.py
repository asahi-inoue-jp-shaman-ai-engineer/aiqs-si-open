"""Explicit synthetic prayer fixtures for transport/ledger regression tests."""
import base64
import io
from PIL import Image
from aiqs_si.service import Service

buffer = io.BytesIO()
Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
PHOTO = base64.b64encode(buffer.getvalue()).decode()
JOY = "自分を大切にする"


def make_service(*args, **kwargs):
    kwargs.setdefault("profile_salt", "synthetic-test-salt")
    return Service(*args, **kwargs)


def target(service, owner):
    return service.register_target(owner, "prayer-fixture-target", name="Synthetic Target",
                                   photo_base64=PHOTO)["target_id"]


def submit_prayer(service, owner, key, intention, target_ids=None):
    return service.submit_scan(owner, key, intention,
                               [target(service, owner)] if target_ids is None else target_ids,
                               self_love_joy=JOY)
