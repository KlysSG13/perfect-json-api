.PHONY: install install-dev test lint run-api run-bot create-key ensure-telegram-key smoke \
        migrate migrate-check revision eval-fake eval-live backup seed-packages verify-docker \
        docker-up docker-down docker-dev-up clean

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements.txt -r requirements-dev.txt

test:
	pytest -q

coverage:
	pytest -q --cov=app --cov-report=term-missing --cov-report=xml
	@if [ -n "$$COVERAGE_FAIL_BELOW" ]; then 		coverage report --fail-under=$$COVERAGE_FAIL_BELOW || 		(echo "COVERAGE_FAIL_BELOW=$$COVERAGE_FAIL_BELOW not reached" && exit 1); 	fi

lint:
	ruff check .
	python -m pyflakes app tests scripts/healthcheck.py scripts/load_test.py
	python -m compileall -q app tests scripts

run-api:
	uvicorn app.api.main:app --reload

run-bot:
	python -m app.bot.main

create-key:
	python -m app.cli create-api-key --name demo --credits 100

ensure-telegram-key:
	python -m app.cli ensure-telegram-key

smoke:
	curl -s http://127.0.0.1:8000/health | python -m json.tool

migrate:
	alembic upgrade head

migrate-check:
	alembic check || (alembic heads && echo "Проверь, что все изменения схемы описаны миграциями")

revision:
	@test -n "$(message)" || (echo 'Использование: make revision message="описание"' && exit 2)
	alembic revision -m "$(message)"

eval-fake:
	python -m app.eval.run --provider fake

eval-live:
	python -m app.eval.run --provider live

backup:
	./scripts/backup_postgres.sh

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

docker-dev-up:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d

clean:
	rm -rf .pytest_cache .ruff_cache __pycache__ app/__pycache__ data/*.db* backups/*.sql.gz

seed-packages:
	python -m app.cli seed-packages

verify-docker:
	./scripts/verify_docker.sh
