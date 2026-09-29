# Security Notes

## Implemented controls

- Argon2 password hashing and forced rotation of temporary credentials.
- Login throttling and temporary lockout.
- Administrator approval before a new account becomes active.
- Unit-scoped authorization on every operational query.
- CSRF protection, secure production cookies and configurable trusted proxy handling.
- Upload size, type and structure validation.
- Defensive XML parsing and duplicate invoice detection.
- Protected document and photo responses instead of direct public media URLs.
- Immutable application audit records reinforced by PostgreSQL triggers.
- Secrets and local credentials excluded by `.gitignore`.
- Automated checks for migrations, static collection, linting and tests.

## Deployment responsibilities

Operators of a fork must supply unique secrets, HTTPS, network isolation, database backups,
retention policies and monitoring. Do not use the CI-only credentials from the workflow outside
the disposable GitHub Actions service container.

## Reporting

Do not open a public issue containing invoice data, personal information, credentials or uploaded
documents. Describe the affected component and a minimal reproduction without sensitive data.
