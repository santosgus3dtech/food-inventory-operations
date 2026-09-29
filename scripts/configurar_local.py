"""Create local configuration without overwriting an existing environment."""

import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
path = root / ".env"
if path.exists():
    print("Configuracao existente preservada.")
else:
    password = secrets.token_urlsafe(36)
    content = (
        f"DJANGO_SECRET_KEY={secrets.token_urlsafe(64)}\n"
        "DJANGO_DEBUG=1\nDJANGO_ALLOWED_HOSTS=127.0.0.1,localhost\n"
        "POSTGRES_DB=foodops\nPOSTGRES_USER=foodops\n"
        f"POSTGRES_PASSWORD={password}\nPOSTGRES_PORT=54329\n"
        f"DATABASE_URL=postgresql://foodops:{password}@127.0.0.1:54329/foodops\n"
    )
    with path.open("x", encoding="utf-8") as stream:
        stream.write(content)
    print("Configuracao local criada. Segredos nao sao versionados.")
