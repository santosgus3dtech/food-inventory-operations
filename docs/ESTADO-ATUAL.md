# Estado atual documental do FoodOps

Revisão: 13/10/2026. Git inicial limpo. Etapa comprovada: inspeção de instruções disponíveis, README, manifesto, fontes arquiteturais/segurança e assinaturas de serviços. `.env`, dados de conta, notas fiscais, uploads, fotos e bancos não foram lidos.

## Componentes e comportamento observados

[pyproject.toml](../pyproject.toml) declara Python ≥3.13 e <3.15, Django 5.2, psycopg, Argon2, django-axes, WhiteNoise e parsers defensivos. A árvore contém `config/`, domínio [core/](../core/), templates/static, deploy e scripts. [stock_services.py](../core/stock_services.py) declara criação/reversão de movimentos, `transaction.atomic` e `select_for_update`; [fiscal_services.py](../core/fiscal_services.py) declara rascunho, confirmação, distribuição e operações transacionais. Isso é evidência estática, não execução de invariantes.

[ARCHITECTURE](ARCHITECTURE.md) mantém domínios de identidade/unidade, estoque, ledger, fiscal, PNAE, cardápio/descarte e auditoria. Descreve reversão como evento compensatório e quantidade como projeção, confirmação fiscal atômica e auditoria protegida por aplicação/triggers. O README apresenta esta cópia como portfólio sanitizado; não se incorporou informação operacional real.

## Regras e segurança documentadas

Consultar [ARCHITECTURE](ARCHITECTURE.md) e [SECURITY](SECURITY.md) antes de mudar estoque, documento fiscal ou autorização. As fontes registram permissões por unidade, aprovação de contas, proteção de documentos/fotos, parsing XML defensivo e DANFE sem alterar estoque antes da conciliação XML. Controles listados na documentação não foram revalidados nesta tarefa.

## Operação

[README](../README.md) mantém a sequência local com uv/Compose/migrações/preparação e o comando `carregar_demo_portfolio`. Qualidade: `uv run ruff check .`, `uv run ruff format --check .`, `manage.py check`, `makemigrations --check --dry-run`, `collectstatic --noinput` e `manage.py test` dentro do ambiente uv. Todos: **não executado nesta tarefa**. Nenhuma configuração, migração ou dado foi criado. [DEMO_WALKTHROUGH](DEMO_WALKTHROUGH.md) continua como roteiro, sem execução atual.

## Plano e retomada

Não foi identificado roadmap aprovado nas fontes consultadas. Não transformar as responsabilidades de implantação do README/SECURITY em nova autorização. Sugestões: (1) definir domínio/fluxo da próxima tarefa; (2) conferir suíte relevante com dados sintéticos quando autorizado; (3) registrar evidência real de invariantes e controle de acesso nas notas afetadas. Nenhum resultado de CI ou prontidão de produção é declarado nesta revisão.

[Índice](INDEX.md).
