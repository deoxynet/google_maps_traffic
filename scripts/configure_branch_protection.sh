#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/configure_branch_protection.sh [--repo owner/name] [--branch branch] [--dry-run]

Options:
  --repo      GitHub repository in owner/name format.
  --branch    Branch to protect. Defaults to the repo default branch.
  --dry-run   Print the API request without applying changes.
  -h, --help  Show this message.

Behavior:
  - Requires: gh, jq
  - Enforces required checks:
      - Rust Checks
      - Fuzz Smoke
EOF
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "missing required command: $cmd" >&2
    exit 1
  fi
}

repo_from_origin() {
  local origin_url
  origin_url="$(git config --get remote.origin.url || true)"
  if [[ -z "$origin_url" ]]; then
    return 1
  fi

  if [[ "$origin_url" =~ ^git@github\.com:([^/]+)/([^/]+?)(\.git)?$ ]]; then
    echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    return 0
  fi
  if [[ "$origin_url" =~ ^https://github\.com/([^/]+)/([^/]+?)(\.git)?$ ]]; then
    echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    return 0
  fi

  return 1
}

REPO=""
BRANCH=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO="${2:-}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

require_cmd gh
require_cmd jq

if [[ -z "$REPO" ]]; then
  REPO="$(repo_from_origin || true)"
fi
if [[ -z "$REPO" ]]; then
  echo "could not determine repository. Pass --repo owner/name or set remote.origin.url." >&2
  exit 1
fi
if [[ "$REPO" != */* ]]; then
  echo "invalid --repo value: $REPO (expected owner/name)" >&2
  exit 1
fi

if [[ -z "$BRANCH" ]]; then
  BRANCH="$(gh api "repos/$REPO" --jq .default_branch)"
fi

if [[ -z "$BRANCH" ]]; then
  echo "could not determine branch. Pass --branch." >&2
  exit 1
fi

mapfile -t CHECK_CONTEXTS < <(printf '%s\n' "Rust Checks" "Fuzz Smoke")
contexts_json="$(printf '%s\n' "${CHECK_CONTEXTS[@]}" | jq -R . | jq -s .)"

payload="$(jq -n \
  --argjson contexts "$contexts_json" \
  '{
    required_status_checks: {
      strict: true,
      contexts: $contexts
    },
    enforce_admins: true,
    required_pull_request_reviews: {
      dismiss_stale_reviews: true,
      require_code_owner_reviews: false,
      required_approving_review_count: 1
    },
    restrictions: null,
    required_linear_history: true,
    allow_force_pushes: false,
    allow_deletions: false,
    block_creations: false,
    required_conversation_resolution: true,
    lock_branch: false,
    allow_fork_syncing: false
  }')"

echo "repo:    $REPO"
echo "branch:  $BRANCH"
echo "checks:  ${CHECK_CONTEXTS[*]}"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run: would call PUT /repos/$REPO/branches/$BRANCH/protection with:"
  echo "$payload"
  exit 0
fi

gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "repos/$REPO/branches/$BRANCH/protection" \
  --input - <<<"$payload" >/dev/null

echo "branch protection updated for $REPO:$BRANCH"
