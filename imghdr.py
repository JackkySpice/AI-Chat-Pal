# Minimal imghdr shim for Python 3.13 compatibility
# Supports jpeg, png, gif detection to satisfy legacy imports
from typing import Optional, Union, BinaryIO


def what(file: Union[str, bytes, bytearray, BinaryIO], h: Optional[bytes] = None) -> Optional[str]:
    """Return 'jpeg', 'png', 'gif' or None based on header bytes.

    Accepts a file path, bytes-like object, or a file-like object.
    """
    header = h

    try:
        if header is None:
            if hasattr(file, "read"):
                # file-like
                pos = file.tell()
                header = file.read(32)
                try:
                    file.seek(pos)
                except Exception:
                    pass
            elif isinstance(file, (bytes, bytearray)):
                header = bytes(file[:32])
            elif isinstance(file, str):
                try:
                    with open(file, "rb") as f:
                        header = f.read(32)
                except Exception:
                    return None
            else:
                return None

        if not header:
            return None

        header = header[:16]
        # JPEG starts with 0xFF 0xD8
        if header.startswith(b"\xff\xd8"):
            return "jpeg"
        # PNG signature
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        # GIF signatures
        if header[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        return None
    except Exception:
        return None