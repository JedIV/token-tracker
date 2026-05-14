.PHONY: setup ingest server agent up down logs

setup:
	uv sync
	uv run pip install -e .

ingest:
	.venv/bin/python -m tracker.ingest -v

agent:
	./scripts/install-launchagent.sh

server:
	./scripts/run-server.sh

# Ensure the periodic-ingest launchd agent is loaded, then serve the UI.
up: agent server

down:
	./scripts/uninstall-launchagent.sh

logs:
	tail -f ~/Library/Logs/token-tracker.log
