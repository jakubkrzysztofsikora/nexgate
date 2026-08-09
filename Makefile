SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

.PHONY: help doctor tools configure demo demo-down demo-smoke validate adapter-contract parity-report

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'
doctor: ## Check no-secret prerequisites without reading .env
	@./scripts/doctor.sh
tools: ## Sync locked validation tooling
	@uv sync --locked --group dev
configure: ## Generate a unique local demo credential without overwriting state
	@./scripts/configure-demo.sh
demo: ## Start the loopback-only mock OpenAI-compatible demo
	@./scripts/demo.sh up
demo-down: ## Stop the isolated local demo
	@./scripts/demo.sh down
demo-smoke: ## Verify the authenticated mock endpoint and chat contract
	@./scripts/demo-smoke.sh
validate: ## Run all offline candidate checks
	@./scripts/validate.sh
adapter-contract: ## Run generic OpenAI-compatible adapter fixtures
	@PYTHONPATH=. uv run pytest -q tests/adapters
parity-report: ## Show migration parity status without reading private credentials
	@./scripts/parity-report.sh
