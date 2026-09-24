.PHONY: audit build check lint release release-patch release-minor release-major security test typecheck

PYTHON ?= python3

# Bump version using commitizen, update changelog, and create a git tag.
# Requires: pip install commitizen

release-patch: check
	cz bump --increment PATCH --changelog
	@echo "Patch release created. Run 'git push --follow-tags' to publish."

release-minor: check
	cz bump --increment MINOR --changelog
	@echo "Minor release created. Run 'git push --follow-tags' to publish."

release-major: check
	cz bump --increment MAJOR --changelog
	@echo "Major release created. Run 'git push --follow-tags' to publish."

# Default: let commitizen decide the bump based on commit messages
release: check
	cz bump --changelog
	@echo "Release created. Run 'git push --follow-tags' to publish."

test:
	$(PYTHON) -m pytest --cov --cov-report=term-missing
	$(PYTHON) -m coverage report --include='*/cli.py' --fail-under=60
	$(PYTHON) -m coverage report --include='*/installer.py' --fail-under=60
	$(PYTHON) -m coverage report --include='*/models/*.py' --fail-under=95
	$(PYTHON) -m coverage report --include='*/webapp.py' --fail-under=80
	$(PYTHON) -m pytest tests/test_ai_config.py tests/test_ai_providers.py tests/test_console.py tests/test_entrypoint_contracts.py --cov=cisco_vmanage_mcp.ai_providers --cov=cisco_vmanage_mcp.app --cov-config=/dev/null --cov-branch --cov-report=term
	$(PYTHON) -m coverage report --include='*/ai_providers.py' --fail-under=60
	$(PYTHON) -m coverage report --include='*/app.py' --fail-under=60

lint:
	$(PYTHON) -m ruff check src tests scripts/live_readonly_audit.py

typecheck:
	$(PYTHON) -m pyright

security:
	$(PYTHON) -m bandit -c pyproject.toml -r src
	$(PYTHON) -m pip_audit -r requirements.txt

build:
	rm -rf build dist
	$(PYTHON) -m build
	$(PYTHON) -m twine check dist/*

check: lint typecheck test

audit: check security build
