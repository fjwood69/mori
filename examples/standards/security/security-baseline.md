# Security Baseline

## Authentication & Authorisation

- All APIs require authentication. No unauthenticated endpoints in
  production except documented health checks.
- API keys must be rotatable. No hardcoded keys in source code.
- OAuth2 / OpenID Connect for user-facing auth. Passwordless where
  possible; bcrypt (cost >= 12) where not.
- Principle of least privilege for service accounts.

## Data Handling

- Encrypt at rest (AES-256-GCM) and in transit (TLS 1.3 minimum).
- No secrets in logs, error messages, or stack traces.
- PII must be pseudonymised in analytics and development environments.
- Retention schedules enforced automatically — no manual cleanup.

## Dependencies

- No deprecated or unmaintained libraries. Monthly audit.
- Vendored dependencies reviewed on upgrade.
- Zero known CVEs in production image at deploy time.

## Incident Response

- All security incidents logged to central monitoring within 60 seconds.
- Pager rotation covers 24/7. Escalation path: engineer on call → security
  lead → CISO.
- Post-mortem within 48 hours. Blameless. Action items tracked to closure.