.PHONY: up down logs test lint format-check type-check check

up:
	docker compose up --build --detach

down:
	docker compose down

logs:
	docker compose logs --follow api outbox-publisher notification-worker

test:
	docker compose --profile test up --build --abort-on-container-exit --exit-code-from test test

lint:
	uv run --extra dev ruff check .

format-check:
	uv run --extra dev ruff format --check .

type-check:
	uv run --extra dev mypy src

check: format-check lint type-check test
