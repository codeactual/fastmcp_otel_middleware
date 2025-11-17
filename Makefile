.PHONY: check_deps
check_deps:
	uv sync --locked --all-groups

.PHONY: install_deps
install_deps:
	uv sync --frozen --all-groups

.PHONY: lint
lint:
	source .venv/bin/activate
	ruff check .
	ruff format --check .
	mypy .

.PHONY: build
build:
	uv build --verbose --color=always --no-cache 2>&1 | grep -Ev "Skipping file for setuptools"

.PHONY: test
test:
	source .venv/bin/activate
	pytest -v --tb=short

.PHONY: ci
ci: check_deps install_deps lint build test
