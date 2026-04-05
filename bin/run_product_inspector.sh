#!/usr/bin/env bash
# Live-product inspection — fetches configured product surfaces and extracts
# structured observations for planning inputs.
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "product_inspector"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -c "
from orchestrator.paths import load_config
from orchestrator.product_inspector import inspect_product, repo_inspection_config
import sys

cfg = load_config()
for project_cfg in cfg.get('github_projects', {}).values():
    if not isinstance(project_cfg, dict):
        continue
    for repo_cfg in project_cfg.get('repos', []):
        slug = repo_cfg.get('github_repo', '')
        path = repo_cfg.get('path', '')
        if not slug or not path:
            continue
        from pathlib import Path
        repo_path = Path(path).expanduser().resolve()
        if not repo_path.is_dir():
            print(f'Skipping {slug}: repo path not found ({path})')
            continue
        icfg = repo_inspection_config(cfg, slug)
        if not icfg.get('enabled'):
            print(f'Skipping {slug}: product inspection disabled')
            continue
        print(f'Inspecting {slug}...')
        result = inspect_product(cfg, slug, repo_path)
        print(f'  Result: {len(result)} chars')
print('Product inspection complete.')
"
