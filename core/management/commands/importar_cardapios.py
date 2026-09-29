import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.menu_pdf import MenuValidationError, parse_menu_pdf
from core.menu_services import create_menu_document


class Command(BaseCommand):
    help = "Importa um ou mais cardápios em PDF como referências semanais."

    def add_arguments(self, parser):
        parser.add_argument("files", nargs="+", help="Caminhos dos PDFs a importar.")
        parser.add_argument("--actor", required=True, help="Usuário administrador responsável.")
        parser.add_argument("--json", action="store_true", help="Exibe o resumo em JSON.")

    def handle(self, *args, **options):
        try:
            actor = get_user_model().objects.get(username=options["actor"].strip().lower())
        except get_user_model().DoesNotExist as exc:
            raise CommandError("Usuário administrador não encontrado.") from exc
        if not actor.has_administrator_role:
            raise CommandError("O responsável informado precisa ser administrador.")

        results = []
        for value in options["files"]:
            path = Path(value).resolve()
            if not path.is_file():
                raise CommandError(f"Arquivo não encontrado: {path}")
            try:
                parsed = parse_menu_pdf(path.read_bytes(), path.name)
            except MenuValidationError as exc:
                raise CommandError(f"{path.name}: {exc}") from exc
            document, created = create_menu_document(parsed, path.name, actor)
            results.append(
                {
                    "file": path.name,
                    "created": created,
                    "document_id": document.pk,
                    "week_start": str(document.week_start or ""),
                    "week_end": str(document.week_end or ""),
                    "meals": document.meals.count(),
                    "status": document.status,
                }
            )

        if options["json"]:
            self.stdout.write(json.dumps(results, ensure_ascii=False))
        else:
            for result in results:
                action = "importado" if result["created"] else "já existente"
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{result['file']}: {action}, {result['meals']} referências."
                    )
                )
