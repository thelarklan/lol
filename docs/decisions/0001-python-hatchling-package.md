# ADR 0001: Package LOL as a Python application with Hatchling

## Status

Accepted for v1.

## Context

LOL needs a conventional installable command, isolated Python dependencies, repeatable builds,
and a contributor workflow that can run on supported Linux hosts without a custom bootstrap tool.

## Decision

Implement LOL for Python 3.11 and newer using a `src/lol/` package. Hatchling builds the standard
wheel and source distribution, while Hatch defines the development, lint, type-check, test, and
build commands. The distribution is named `launch-on-local` and exposes the `lol` console script.

Recommend pipx for end-user installation. Support installation into a normal virtual environment
as the fallback; pipx is not a runtime dependency.

## Consequences

- Releases use standard Python package artifacts and metadata.
- Contributors may use Hatch or invoke the underlying Python tools directly.
- Users need Python 3.11 or newer, but do not need Hatch at runtime.
- Public PyPI publication can remain gated until the full Jenkins fixture suite passes.
