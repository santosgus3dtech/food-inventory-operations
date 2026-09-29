from datetime import timedelta
from getpass import getpass

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import User
from core.services import audit, lock_user_management


class Command(BaseCommand):
    help = "Recuperação assistida no terminal LOCAL; senha nunca aparece na saída."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        if not settings.DEBUG or settings.DATABASES["default"]["HOST"] not in (
            "localhost",
            "127.0.0.1",
        ):
            raise CommandError("Comando disponível apenas no ambiente local.")
        with transaction.atomic():
            lock_user_management()
            try:
                user = User.objects.select_for_update().get(username=options["username"].lower())
            except User.DoesNotExist as exc:
                raise CommandError("Usuário não encontrado.") from exc
            password = getpass("Nova senha temporária: ")
            if password != getpass("Repita a senha: "):
                raise CommandError("As senhas não coincidem.")
            try:
                validate_password(password, user)
            except ValidationError as exc:
                raise CommandError("; ".join(exc.messages)) from exc
            user.set_password(password)
            user.must_change_password = True
            user.temporary_password_expires_at = timezone.now() + timedelta(hours=24)
            user.save()
            audit(None, user, "password_reset", description="Senha redefinida pelo terminal local")
        self.stdout.write(
            "Senha temporária alterada. O arquivo de acesso inicial não é atualizado."
        )
