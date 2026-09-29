# Architecture

## Boundaries

The application uses a conventional Django structure with explicit domain services for operations
that must be atomic or reusable. Views coordinate HTTP concerns; stock, invoice, menu, image and
audit rules live under `core/`.

## Main domains

- **Identity and access:** custom users, administrator approval, unit membership and role checks.
- **Inventory:** foods, packages, unit assignments, thresholds and current quantities.
- **Stock ledger:** append-only movements with reversals and transactional balance updates.
- **Fiscal intake:** NF-e XML and DANFE PDF validation, matching, drafts and distribution.
- **Compliance:** annual PNAE catalogs and approval of justified exceptions.
- **Operations:** weekly menus, disposal records, protected photos and price history.
- **Audit:** before/after values, actor, action, object and request context.

## Consistency strategy

Stock-affecting operations run inside `transaction.atomic()` and lock the relevant rows before
changing quantities. The current balance is a fast projection; the ledger remains the source of
accountability. A reversal references the original movement and creates a compensating movement.

Invoice confirmation validates that distributed quantities equal the converted invoice quantity.
Every unit entry is committed together. A failure rolls the whole confirmation back.

Audit records are protected from update and delete by both application rules and PostgreSQL
triggers. Database superusers still retain administrative authority, so production access to that
role must be restricted.

## File handling

- XML uses defensive parsing and structural limits.
- PDFs have size and page limits and never change stock without structured XML.
- Uploaded photos are decoded, normalized and re-encoded before storage.
- Private documents are returned only through permission-checked views.

## Deployment

The production shape is a Django/Gunicorn container connected to a private PostgreSQL network and
an HTTPS reverse proxy. The web container can run with a read-only root filesystem and a temporary
`/tmp`. PostgreSQL should not publish a public port.
