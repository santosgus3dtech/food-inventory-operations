import hashlib
import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_DISPOSAL_PHOTO_SIZE = 8 * 1024 * 1024
MAX_DISPOSAL_PHOTO_PIXELS = 24_000_000
ALLOWED_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", "JPEG"),
    "PNG": ("image/png", "PNG"),
    "WEBP": ("image/webp", "WEBP"),
}


class DisposalImageError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedDisposalPhoto:
    original_name: str
    content_type: str
    content: bytes
    sha256: str
    width: int
    height: int


def _safe_filename(value):
    value = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return value[:180] or "foto-do-descarte"


def prepare_disposal_photo(content, original_name):
    if not content:
        raise DisposalImageError("A foto está vazia.")
    if len(content) > MAX_DISPOSAL_PHOTO_SIZE:
        raise DisposalImageError("A foto ultrapassa o limite de 8 MB.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as candidate:
                candidate.verify()
            with Image.open(BytesIO(content)) as source:
                source.load()
                image_format = source.format
                if image_format not in ALLOWED_IMAGE_FORMATS:
                    raise DisposalImageError("Use uma foto JPEG, PNG ou WebP.")
                image = ImageOps.exif_transpose(source)
                width, height = image.size
                if width * height > MAX_DISPOSAL_PHOTO_PIXELS:
                    raise DisposalImageError("A foto possui resolução maior que 24 megapixels.")

                content_type, output_format = ALLOWED_IMAGE_FORMATS[image_format]
                output = BytesIO()
                if output_format == "JPEG":
                    if image.mode != "RGB":
                        image = image.convert("RGB")
                    image.save(output, "JPEG", quality=90, optimize=True, progressive=True)
                elif output_format == "PNG":
                    image.save(output, "PNG", optimize=True)
                else:
                    if image.mode not in ("RGB", "RGBA"):
                        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                    image.save(output, "WEBP", quality=90, method=6)
    except DisposalImageError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise DisposalImageError("Não foi possível validar esta foto.") from exc

    normalized = output.getvalue()
    return PreparedDisposalPhoto(
        original_name=_safe_filename(original_name),
        content_type=content_type,
        content=normalized,
        sha256=hashlib.sha256(normalized).hexdigest(),
        width=width,
        height=height,
    )
