# AGENT.md

## Testing style

Prefer a Given-When-Then structure for tests. Use clear comments or variable names
to separate setup, action, and assertions, especially when the behavior is not
obvious from the test name alone.

## Python dependencies

When adding Python dependencies, define an upper bound to the next major release
(e.g. `package>=1.2,<2`).

## Lint suppressions

Get user approval before adding `# noqa` suppressions to code.

## Type safety

Avoid using `Any` when a more precise type is practical, especially in protocol
implementations and test fakes. `Any` weakens mypy/pyright protocol conformance
checks and can hide interface drift. Prefer domain types, `Protocol`s,
`Mapping`/`Sequence`, or explicit JSON-like type aliases over broad `Any`.

When adding or changing a `Protocol`, a real implementation of a protocol, or a
test fake intended to satisfy a protocol, update `tests/test_protocol_conformance.py`
so mypy/pyright explicitly verify that implementation against the intended
interface.
