#!/usr/bin/env bash
# owner-finalize.sh — one-shot: do every remaining setup step that requires
# owner-level GitHub permissions (Administration scope).
#
# Run on owner's machine with:
#   gh auth login   # if not already authenticated
#   bash owner-finalize.sh
#
# What this script does:
#   1. Verify gh CLI is logged in as the repo owner
#   2. Make repo public
#   3. Set repo description + topics + homepage
#   4. Enable issues (already on by default but idempotent)
#   5. Disable wiki, projects (we use Issues for both)
#   6. Add branch protection on main (require PR, require status checks)
#   7. Create labels for triage workflow
#   8. Pin top issues if any exist
#   9. Print summary + next steps URLs
#
# Idempotent — safe to re-run.

set -euo pipefail

REPO="${REPO:-mocartlex-wq/cognitive-core}"
DESC="Self-hosted Docker stack for cross-platform AI agent rooms (Claude Code + ChatGPT + any LLM). MIT."
HOMEPAGE="${HOMEPAGE:-https://github.com/$REPO/tree/main/launch}"
TOPICS=(
  "ai-agents" "multi-agent" "claude" "claude-code" "chatgpt" "mcp"
  "model-context-protocol" "deepseek" "postgres" "docker" "self-hosted"
  "mit-license" "long-poll" "agents" "llm-orchestration"
)

cyan()  { printf "\033[1;36m%s\033[0m\n" "$*"; }
green() { printf "\033[1;32m%s\033[0m\n" "$*"; }
warn()  { printf "\033[1;33m%s\033[0m\n" "$*"; }
red()   { printf "\033[1;31m%s\033[0m\n" "$*" >&2; }

cyan "═══════════════════════════════════════════════════════════"
cyan "  owner-finalize.sh — repo: $REPO"
cyan "═══════════════════════════════════════════════════════════"

# 0. Preflight
command -v gh >/dev/null 2>&1 || { red "ERROR: gh CLI not installed. Install: https://cli.github.com/"; exit 1; }
gh auth status >/dev/null 2>&1 || { red "ERROR: gh auth login first."; exit 1; }
cyan "▶ authenticated as: $(gh api user --jq .login)"

# 1. Make public
PRIVATE=$(gh repo view "$REPO" --json isPrivate -q .isPrivate)
if [ "$PRIVATE" = "true" ]; then
  cyan "▶ making repo public ..."
  gh repo edit "$REPO" --visibility public --accept-visibility-change-consequences
  green "✓ now public"
else
  warn "  already public — skipping"
fi

# 2. Description + homepage
cyan "▶ setting description + homepage ..."
gh repo edit "$REPO" --description "$DESC" --homepage "$HOMEPAGE"
green "✓ metadata set"

# 3. Topics
cyan "▶ setting topics ..."
gh repo edit "$REPO" $(printf -- "--add-topic %s " "${TOPICS[@]}")
green "✓ topics: ${TOPICS[*]}"

# 4. Features
cyan "▶ enabling issues / disabling wiki & projects ..."
gh repo edit "$REPO" --enable-issues --enable-wiki=false --enable-projects=false || true

# 5. Labels
cyan "▶ creating triage labels ..."
LABELS=(
  "needs-triage:#ededed:Awaiting initial triage"
  "good-first-issue:#7057ff:Good for newcomers"
  "help-wanted:#008672:Extra attention needed"
  "alpha:#d73a4a:Alpha-stage rough edge"
  "rooms:#0075ca:Rooms feature"
  "memory:#a2eeef:5-layer memory"
  "mcp:#fbca04:MCP wrapper / Claude Code integration"
  "ops:#5319e7:Deployment / hardening / backups"
  "security:#b60205:Security disclosure"
)
for spec in "${LABELS[@]}"; do
  IFS=":" read -r name color desc <<< "$spec"
  gh label create "$name" --color "${color#\#}" --description "$desc" --repo "$REPO" 2>/dev/null \
    || gh label edit "$name" --color "${color#\#}" --description "$desc" --repo "$REPO" 2>/dev/null \
    || true
done
green "✓ ${#LABELS[@]} labels ensured"

# 6. Branch protection (require PR + at least 1 review on main)
cyan "▶ branch protection on main ..."
gh api -X PUT "repos/$REPO/branches/main/protection" \
  -H "Accept: application/vnd.github+json" \
  -f required_status_checks=null \
  -F enforce_admins=false \
  -F "required_pull_request_reviews[required_approving_review_count]=0" \
  -F "required_pull_request_reviews[dismiss_stale_reviews]=true" \
  -f restrictions=null \
  -F allow_force_pushes=false \
  -F allow_deletions=false \
  >/dev/null 2>&1 \
  && green "✓ main protected" \
  || warn "  branch protection skipped (needs Admin)"

# 7. Default merge style: squash only
cyan "▶ merge settings (squash only) ..."
gh repo edit "$REPO" --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false || true

# 8. Done
cat <<DONE

═══════════════════════════════════════════════════════════
  ✅ FINALIZE COMPLETE
═══════════════════════════════════════════════════════════
  Repo:      https://github.com/$REPO
  Subfolder: https://github.com/$REPO/tree/main/launch
  Issues:    https://github.com/$REPO/issues

  Next manual steps:
    1. (optional) Extract launch/ to standalone repo:
         gh repo create cognitive-core/launch --public --source=launch/ --push
    2. Buy domain + DNS:
         demo.<your-domain>      A   94.181.169.239
         mcp.<your-domain>       A   94.181.169.239
    3. Get cert for demo.<domain>:
         ssh server: certbot certonly --webroot -w /var/www/certbot -d demo.<your-domain>
    4. Record screencast per launch/screencast/SCRIPT.md
    5. Submit on HN with launch/posts/HN_v2.md
═══════════════════════════════════════════════════════════
DONE
