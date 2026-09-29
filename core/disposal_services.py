from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .access import visible_units
from .disposal_images import DisposalImageError, prepare_disposal_photo
from .models import DisposalPhoto, DisposalRecord, Unit
from .services import audit


def create_disposal_record(*, unit, occurred_on, observation, uploads, actor):
    if not visible_units(actor).filter(pk=unit.pk, is_active=True).exists():
        raise PermissionDenied

    prepared = []
    for uploaded in uploads:
        try:
            prepared.append(prepare_disposal_photo(uploaded.read(), uploaded.name))
        except DisposalImageError as exc:
            raise ValidationError(f"{uploaded.name}: {exc}") from exc
    hashes = [photo.sha256 for photo in prepared]
    if len(hashes) != len(set(hashes)):
        raise ValidationError("Não envie a mesma foto mais de uma vez no mesmo registro.")

    with transaction.atomic():
        locked_unit = Unit.objects.select_for_update().get(pk=unit.pk, is_active=True)
        record = DisposalRecord.objects.create(
            unit=locked_unit,
            occurred_on=occurred_on,
            observation=observation.strip(),
            created_by=actor,
        )
        DisposalPhoto.objects.bulk_create(
            [
                DisposalPhoto(
                    record=record,
                    original_name=photo.original_name,
                    content_type=photo.content_type,
                    image_content=photo.content,
                    image_sha256=photo.sha256,
                    width=photo.width,
                    height=photo.height,
                    size=len(photo.content),
                )
                for photo in prepared
            ]
        )
        audit(
            actor,
            record,
            "disposal_created",
            unit=locked_unit,
            description=(f"Descarte registrado em {locked_unit.code} com {len(prepared)} foto(s)"),
        )
    return record
