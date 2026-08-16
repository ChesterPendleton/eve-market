.PHONY: setup test lint fmt up down doctor clean

setup:
	./setup.sh

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/python -m ruff check .

fmt:
	.venv/bin/python -m ruff check --fix .
	.venv/bin/python -m ruff format .

up:
	docker compose up -d

down:
	docker compose down

doctor:
	.venv/bin/eve-market doctor

clean:
	rm -rf .venv .pytest_cache .ruff_cache data
	find . -name __pycache__ -type d -exec rm -rf {} +
