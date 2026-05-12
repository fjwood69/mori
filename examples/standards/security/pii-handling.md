# PII Handling

## Classification

| Level | Examples | Controls |
|-------|----------|----------|
| Public | Company name, job title | No restrictions |
| Internal | Email, employee ID | Access-controlled, no external sharing |
| Sensitive | Home address, DOB, health data | Encrypted at rest, audited access, 90-day retention |
| Regulated | Passport, biometrics, financial | Dedicated storage, full audit trail, legal hold |

## Rules

1. **Never log PII** — Structured logging must strip fields matching
   patterns (email, phone, postcode). Test this with CI.
2. **Never send PII to LLMs** — If PII may be present in the input,
   sanitise before passing to any model API. No exceptions.
3. **Mask in dev** — Development databases use synthetic or masked
   data. Prod data never reaches a dev environment.
4. **Right to erasure** — `/api/user/data` endpoint must delete all
   PII within 30 days of request. Document the scope.
5. **Data Protection Impact Assessment** required before processing any
   new category of personal data.