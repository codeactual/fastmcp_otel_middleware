

.PHONY: check_deps
check_deps:
	uv sync --locked --all-groups

.PHONY: install_deps
install_deps:
	uv sync --frozen --all-groups

.PHONY: lint
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy .

.PHONY: build
build:
	uv build --verbose --color=always --no-cache 2>&1 | grep -Ev "Skipping file for setuptools"

.PHONY: test
test:
	uv run pytest -v --tb=short

.PHONY: ci
ci: check_deps install_deps lint build test
