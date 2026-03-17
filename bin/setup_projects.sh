#!/usr/bin/env bash
# Create a separate GitHub Project for each repo and print the project numbers
# to update in config.yaml.
#
# Usage: ./bin/setup_projects.sh

set -euo pipefail

OWNER="kai-linux"

declare -A REPOS=(
    ["writeaibook"]="kai-linux/bookgenerator"
    ["agent-os"]="kai-linux/agent-os"
    ["content-automation"]="kai-linux/content-automation"
    ["kdp"]="kai-linux/kdp"
    ["browser-automation"]="kai-linux/browser-automation"
)

echo "Creating GitHub Projects for each repo..."
echo ""

for key in "${!REPOS[@]}"; do
    repo="${REPOS[$key]}"
    title="$key"

    # Check if project already exists
    existing=$(gh project list --owner "$OWNER" --format json | python3 -c "
import json, sys
data = json.load(sys.stdin)
items = data.get('projects', data) if isinstance(data, dict) else data
for p in items:
    if p.get('title') == '$title':
        print(p.get('number', ''))
        break
" 2>/dev/null || true)

    if [ -n "$existing" ]; then
        echo "  $key: project #$existing already exists"
        proj_num="$existing"
    else
        echo "  $key: creating project '$title'..."
        proj_num=$(gh project create --owner "$OWNER" --title "$title" --format json | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(data.get('number', ''))
")
        echo "  $key: created project #$proj_num"
    fi

    echo ""
    echo "  Update config.yaml:"
    echo "    $key:"
    echo "      project_number: $proj_num"
    echo "      title: \"$title\""
    echo ""
done

echo "Done. Update config.yaml with the project numbers above."
echo "Then add your issues to the correct project board and set Status to 'Ready'."
