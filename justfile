install:
    uv sync --no-install-project --no-default-groups

install-dev:
    uv sync --no-install-project

# Link an account; optional arguments select an existing SSH alias or address
codex-login profile *args:
    uv run --frozen python tools/codex_usage/control.py login {{quote(profile)}} {{args}}

# Show enrolled accounts and their Home Assistant sensor IDs
codex-status *args:
    uv run --frozen python tools/codex_usage/control.py status {{args}}

# Prepare account sections for the storage-mode Tooling dashboard
codex-dashboard *args:
    uv run --frozen python tools/codex_usage/control.py dashboard {{args}}
