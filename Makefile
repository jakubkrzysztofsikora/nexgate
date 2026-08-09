SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

.PHONY: help doctor tools hooks secrets release-audit configure configure-demo up down observability demo demo-down demo-smoke validate adapter-contract adapter-diagnostic parity-report

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'
doctor: ## Check no-secret prerequisites without reading .env
	@./scripts/doctor.sh
tools: ## Sync locked validation tooling
	@uv sync --locked --group dev
hooks: ## Install local pre-commit secret prevention hooks
	@pre-commit install --install-hooks
secrets: ## Scan tracked content and reachable history for secrets
	@./scripts/secret-scan.sh all
release-audit: ## Run release-time validation, archive scan, and checksums
	@./scripts/release-audit.sh
configure: ## Create an ignored operator configuration with generated local secrets
	@./scripts/configure-stack.sh
configure-demo: ## Create an isolated credential for the loopback demo
	@./scripts/configure-demo.sh
up: ## Render configured provider routes and start the NexGate stack
	@./scripts/stack.sh up
down: ## Stop the NexGate stack while preserving local volumes
	@./scripts/stack.sh down
observability: ## Start NexGate with Prometheus and Grafana profiles
	@./scripts/stack.sh observability
demo: ## Start the loopback-only mock OpenAI-compatible demo
	@./scripts/demo.sh up
demo-down: ## Stop the isolated local demo
	@./scripts/demo.sh down
demo-smoke: ## Verify the authenticated mock endpoint and chat contract
	@./scripts/demo-smoke.sh
validate: ## Run all offline repository checks
	@./scripts/validate.sh
adapter-contract: ## Run generic OpenAI-compatible adapter fixtures
	@PYTHONPATH=. uv run pytest -q tests/adapters
adapter-diagnostic: ## Manually probe an explicitly selected local provider overlay
	@PYTHONPATH=. uv run python -m gateway.diagnostic
parity-report: ## Show migration parity status without reading private credentials
	@./scripts/parity-report.sh
