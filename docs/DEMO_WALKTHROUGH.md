# Portfolio demo walkthrough

This scenario uses only fictional units, foods, lots and quantities. It is designed to make the operational workflow visible without importing a private database or fiscal document.

## Prepare the local environment

```powershell
uv sync --locked
uv run python scripts/configurar_local.py
docker compose up -d --wait
uv run python manage.py migrate
uv run python manage.py preparar_local
uv run python manage.py carregar_demo_portfolio
uv run python manage.py runserver 127.0.0.1:8000 --noreload
```

The demo command refuses to run outside `DEBUG`, refuses remote database hosts and refuses a database that already contains units.

## Guided scenario

1. Open the dashboard and compare the three fictional units.
2. Select **Unidade Demo Centro** and review minimum-stock indicators.
3. Open the movement ledger. Each unit starts with one synthetic receipt and one weekly consumption event.
4. Register a small consumption for rice and milk, then verify the resulting balance and audit event.
5. Reverse the movement as an administrator. The original event remains immutable and a compensating event is created.
6. Use the inventory filters to locate products below their configured minimum.

## Recruiter takeaway

The walkthrough demonstrates transaction boundaries, an immutable stock ledger, compensating reversals, role-aware actions, auditability and a reproducible synthetic dataset.
