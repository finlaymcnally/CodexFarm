#!/usr/bin/env bash
# How to run:
#   bash docs/build-docs-summary.sh
# Output:
#   docs/<timestamp>_<root_folder_name>-docs-summary.md
# Note:
#   Only .md and .txt files are included in the generated summary.
#   Files ending with "_log.md" are skipped.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docs_dir="${repo_root}/docs"
repo_name="$(basename "$repo_root")"
timestamp="$(date +"%Y-%m-%d_%H.%M.%S")"
output="${docs_dir}/${timestamp}_${repo_name}-docs-summary.md"

if [[ ! -d "$docs_dir" ]]; then
  echo "Docs directory not found: $docs_dir" >&2
  exit 1
fi

mapfile -d '' files < <(find "$docs_dir" -type f -print0 | sort -z)

{
  cat <<EOF
---
summary: "Combined snapshot of docs/ as of ${timestamp}."
read_when:
  - When you need a single-file snapshot of the docs tree.
---
# ${repo_name} Docs Summary
Generated: ${timestamp}
Source root: docs/

EOF

  for file in "${files[@]}"; do
    if [[ "$file" == "$output" ]]; then
      continue
    fi

    # Skip architecture/build/fix-attempt log docs.
    if [[ "$file" == *"_log.md" ]]; then
      continue
    fi

    ext="${file##*.}"
    ext_lower="$(printf "%s" "$ext" | tr '[:upper:]' '[:lower:]')"
    
    # Only summarize .md and .txt files
    case "$ext_lower" in
      md|txt) ;;
      *) continue ;;
    esac

    # Skip previous summary files to avoid recursive inclusion.
    if [[ "$file" == *"-docs-summary.md" ]]; then
      continue
    fi

    rel="${file#$repo_root/}"
    echo "## ${rel}"
    echo ""

    case "$ext_lower" in
      md) lang="markdown" ;;
      txt) lang="text" ;;
      *) lang="" ;;
    esac

    printf '```%s\n' "$lang"
    cat "$file"
    printf '```\n'
    echo ""
  done
} > "$output"

echo "Wrote $output"
