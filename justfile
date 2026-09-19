set shell := ["bash", "-cu"]

# Run the test suite.
test:
    uv run pytest

# Run formatting checks, linting, and static type checks.
lint:
    uv run ruff format --check .
    uv run ruff check .
    uv run mypy
    uv run pyright
    uv run pyright --verifytypes jenkins_stats --ignoreexternal

# Format Python code.
format:
    uv run ruff format .

# Run all CI-style checks.
check: lint test

# Remove untracked files while preserving jj metadata.
clean:
    git clean -fxd -e .jj
