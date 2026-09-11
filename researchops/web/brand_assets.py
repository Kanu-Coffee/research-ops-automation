"""Public fixed brand assets; never a directory browser or session endpoint."""

from pathlib import Path
import hashlib


ASSETS = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/assets/brand/icon-32.png": ("icon-32.png", "image/png"),
    "/assets/brand/icon-16.png": ("icon-16.png", "image/png"),
    "/apple-touch-icon.png": ("icon-180.png", "image/png"),
}


def asset_response(path):
    entry = ASSETS.get(path)
    if entry is None:
        return None
    filename, content_type = entry
    content = (Path(__file__).parent / "assets" / "brand" / filename).read_bytes()
    return 200, {"Content-Type": content_type, "Cache-Control": "public, max-age=3600",
                 "ETag": '"' + hashlib.sha256(content).hexdigest() + '"'}, content
