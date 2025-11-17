.PHONY: check_deps
check_deps:
	uv sync --locked

.PHONY: install_deps
install_deps:
	uv sync --frozen

.PHONY: lint
lint:
	ruff check .
	ruff format --check .
	mypy .

.PHONY: build
build:
	uv build --verbose --color=always --no-cache 2>&1 | grep -Ev "Skipping file for setuptools"

.PHONY: test
test:
	pytest -v --tb=short

.PHONY: ci
ci: check_deps install_deps lint build test
