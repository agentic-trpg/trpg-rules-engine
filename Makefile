# Shared local/CI validation. BASE is an optional exact Git comparison commit.
VALIDATE = uv run --no-project python tools/validate.py
BASE_ARG = $(if $(BASE),--base $(BASE),)
.PHONY: check-fast check-integration check check-plan check-auto docs smoke \
        check-engine check-srd-data check-bridge check-demo examples format

# Ordinary batches: scoped lint, types and runtime tests without coverage.
check-fast:
	$(VALIDATE) fast $(BASE_ARG)

# Includes Fast once; use INSTEAD OF check-fast for public/high-risk changes.
check-integration:
	$(VALIDATE) integration $(BASE_ARG)

# CI uses exactly the same planner, automatically escalating high-risk changes.
check-auto:
	$(VALIDATE) fast --auto $(BASE_ARG)

check-plan:
	$(VALIDATE) fast --plan $(BASE_ARG)

# Original package coverage/security gates, plus docs and clean-wheel smoke.
check:
	$(VALIDATE) full $(BASE_ARG)

docs:
	$(VALIDATE) docs

check-srd-data:
	$(MAKE) -C packages/dnd5e-srd-data check

check-engine:
	$(MAKE) -C packages/dnd5e-engine check

check-bridge:
	$(MAKE) -C packages/nat20-bridge check

check-demo:
	$(MAKE) -C apps/demo check

# Runnable examples double as an integration smoke over the public API.
examples:
	uv run python examples/grid_combat.py
	uv run python examples/skill_check.py
	uv run python examples/build_party_member.py

# Portable clean-wheel smoke, included once in Full.
smoke:
	$(VALIDATE) smoke

# Auto-apply formatting across both packages.
format:
	$(MAKE) -C packages/dnd5e-engine format
	cd packages/dnd5e-srd-data && uv run ruff format src tests tools
	cd packages/nat20-bridge && uv run ruff format src tests
	cd apps/demo && uv run ruff format src tests
	uv run ruff format tools
