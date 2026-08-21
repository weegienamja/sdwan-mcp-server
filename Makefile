.PHONY: release release-patch release-minor release-major

# Bump version using commitizen, update changelog, and create a git tag.
# Requires: pip install commitizen

release-patch:
	cz bump --increment PATCH --changelog
	@echo "Patch release created. Run 'git push --follow-tags' to publish."

release-minor:
	cz bump --increment MINOR --changelog
	@echo "Minor release created. Run 'git push --follow-tags' to publish."

release-major:
	cz bump --increment MAJOR --changelog
	@echo "Major release created. Run 'git push --follow-tags' to publish."

# Default: let commitizen decide the bump based on commit messages
release:
	cz bump --changelog
	@echo "Release created. Run 'git push --follow-tags' to publish."

test:
	pytest -v

lint:
	python -m py_compile src/cisco_vmanage_mcp/server.py
	python -m py_compile src/cisco_vmanage_mcp/cli.py
	python -m py_compile src/cisco_vmanage_mcp/installer.py
	python -m py_compile src/cisco_vmanage_mcp/telemetry.py
