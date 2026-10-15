.PHONY: init up down logs ps migrate test backup restore-check restore

init:            ## generate .env and secrets/htpasswd with random secrets
	./scripts/init-env.sh

up:              ## build and start everything
	docker compose up -d --build

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f --tail=100 api traefik

migrate:         ## apply database migrations manually (also runs automatically on `up`)
	docker compose run --rm migrate

backup:          ## dump PostgreSQL to ./backups (+ off-site copy if BACKUP_RCLONE_REMOTE is set)
	./scripts/backup.sh

restore-check:   ## safe restore drill: make restore-check FILE=backups/shortener-XXXX.dump
	./scripts/restore.sh --check $(FILE)

restore:         ## DESTRUCTIVE: replace the live database: make restore FILE=backups/shortener-XXXX.dump
	./scripts/restore.sh $(FILE)

test:            ## needs PostgreSQL + Redis, see tests/conftest.py
	pip install -q -r requirements-dev.txt
	pytest
