import secrets
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import User
from core.services import audit, lock_user_management


class Command(BaseCommand):
    help = "Cria um administrador LOCAL com senha aleatória em arquivo ignorado pelo Git."

    def handle(self, *args, **options):
        if not settings.DEBUG or settings.DATABASES["default"]["HOST"] not in (
            "localhost",
            "127.0.0.1",
        ):
            raise CommandError(
                "Este comando só pode ser usado no ambiente local de desenvolvimento."
            )
        with transaction.atomic():
            lock_user_management()
            if User.objects.filter(is_active=True, is_general_admin=True).exists():
                self.stdout.write("Administrador existente preservado.")
                return
            if User.objects.filter(username="admin").exists():
                raise CommandError("Já existe o usuário admin. Recupere a conta existente.")
            password = secrets.token_urlsafe(20)
            user = User.objects.create_user(
                username="admin",
                password=password,
                first_name="Administrador",
                is_general_admin=True,
                must_change_password=True,
                temporary_password_expires_at=timezone.now() + timedelta(hours=24),
            )
            audit(None, user, "created", description="Administrador local inicial")
            folder = settings.BASE_DIR / ".local"
            folder.mkdir(exist_ok=True)
            with (folder / "acesso-inicial.txt").open("x", encoding="utf-8") as stream:
                stream.write(
                    "FoodOps Estoque — acesso LOCAL\n\n"
                    "Endereço: http://127.0.0.1:8000\nUsuário: admin\n"
                    f"Senha temporária: {password}\n\n"
                    "A senha expira em 24 horas e deve ser alterada no primeiro acesso.\n"
                    "Não compartilhe este arquivo nem o inclua no Git.\n"
                )
        self.stdout.write("Administrador criado. Dados de acesso em .local/acesso-inicial.txt")
