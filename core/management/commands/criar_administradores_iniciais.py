import os
import secrets
import string
from datetime import timedelta
from pathlib import Path

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import User
from core.services import audit, lock_user_management


def secure_temporary_password(user):
    alphabet = string.ascii_letters + string.digits + "!@#%*-_"
    for _ in range(100):
        parts = [
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits),
            secrets.choice("!@#%*-_"),
        ]
        parts.extend(secrets.choice(alphabet) for _ in range(16))
        secrets.SystemRandom().shuffle(parts)
        password = "".join(parts)
        try:
            validate_password(password, user)
        except ValidationError:
            continue
        return password
    raise CommandError("Não foi possível gerar uma senha temporária válida.")


class Command(BaseCommand):
    help = "Cria administradores com senhas temporárias e salva as credenciais fora do Git."

    def add_arguments(self, parser):
        parser.add_argument("usernames", nargs="+")
        parser.add_argument("--output", required=True)

    def handle(self, *args, **options):
        usernames = [value.strip().lower() for value in options["usernames"]]
        if len(usernames) != len(set(usernames)) or any(not value for value in usernames):
            raise CommandError("Informe nomes de usuário únicos e não vazios.")

        output = Path(options["output"]).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise CommandError("O arquivo de credenciais já existe; nada foi alterado.")

        credentials = []
        with transaction.atomic():
            lock_user_management()
            for username in usernames:
                if User.objects.filter(username=username).exists():
                    raise CommandError(f"O usuário {username} já existe; nada foi alterado.")
            for username in usernames:
                user = User(username=username, is_active=True, is_general_admin=True)
                password = secure_temporary_password(user)
                user.set_password(password)
                user.must_change_password = True
                user.temporary_password_expires_at = timezone.now() + timedelta(hours=24)
                user.save()
                audit(None, user, "created", description=f"Administrador inicial: {username}")
                credentials.append((username, password))

        try:
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write("FoodOps Estoque — administradores iniciais\n")
                stream.write(
                    "As senhas expiram em 24 horas e devem ser trocadas no primeiro acesso.\n\n"
                )
                for username, password in credentials:
                    stream.write(f"Usuário: {username}\nSenha temporária: {password}\n\n")
        except OSError as exc:
            raise CommandError(
                "As contas foram criadas, mas o arquivo de credenciais falhou."
            ) from exc

        self.stdout.write(
            f"{len(credentials)} administradores criados. Credenciais salvas no arquivo informado."
        )
