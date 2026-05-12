# Python Style Guide

## Formatting

- Use `ruff` for formatting and linting. Line length: 100.
- Type hints on all public functions and methods.
- Docstrings: Google-style for public APIs, omitted for trivial
  internals where the name and types are self-documenting.

## Conventions

- Prefer `pathlib.Path` over `os.path`. Prefer `shutil` over `subprocess`
  for file operations.
- Context managers (`with`) for all external resources (files, sockets,
  database connections).
- No wildcard imports (`from x import *`). Explicit is better than implicit.
- Logging over print. Use structured loggers, not f-strings in log calls.

## Testing

- `pytest` with `pytest-cov`. Coverage >= 80%.
- Tests must not depend on external services unless marked `@pytest.mark.integration`.
- Fixtures over setup functions. Parametrise over copy-paste.

## Error Handling

- Raise specific exceptions, not bare `Exception`. Catch specific
  exceptions, not bare `except:`.
- Fail fast on invalid input. Validate at the boundary, not in the middle.