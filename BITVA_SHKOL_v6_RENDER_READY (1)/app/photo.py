from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from .config import MEDIA_BACKEND, MEDIA_ROOT

ALLOWED={"JPEG","PNG","WEBP"}


def sanitize_image(data: bytes, max_side: int = 2400) -> tuple[bytes,str]:
    with Image.open(BytesIO(data)) as im:
        if im.format not in ALLOWED:
            raise ValueError("Поддерживаются JPG, PNG и WEBP")
        im = im.convert("RGB")
        im.thumbnail((max_side,max_side))
        out=BytesIO(); im.save(out,"JPEG",quality=88,optimize=True)
        return out.getvalue(), "image/jpeg"


def save_final(institution_id: int, kind: str, data: bytes, mime: str) -> str:
    root=Path(MEDIA_ROOT)/"institutions"/str(institution_id)
    root.mkdir(parents=True,exist_ok=True)
    path=root/f"{kind}.jpg"
    path.write_bytes(data)
    return str(path)
