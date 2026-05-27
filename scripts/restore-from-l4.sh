#!/usr/bin/env bash
# restore-from-l4.sh — restore Cognitive Core data из L4 MinIO snapshots в Postgres.
#
# Снапшоты в L4 содержат ТОЛЬКО L3-данные (knowledge + tools) — формат JSON,
# структура: {"knowledge": [...], "tools": [...], "hash": "...", "created_at": "..."}.
# Path в MinIO: l4/<owner_user_id>/<domain>/<snapshot_uuid>.json
# (legacy путь без owner: l4/<domain>/<snapshot_uuid>.json — тоже обрабатываем).
#
# L1 / L2 — НЕ восстанавливаются из L4 (их в снапшотах нет). Для L1/L2 нужен
# pg_dump через scripts/cron-backup.sh — см. docs_restore_design.md.
#
# Usage:
#   sudo bash restore-from-l4.sh \
#       --date YYYY-MM-DD \
#       --owner <OWNER_USER_UUID> \
#       --target-db "postgres://user:pw@host:5432/db" \
#       [--layer l3|all] [--domain <name>] [--dry-run] [--force]
#
# Exit codes: 0 OK, 1 args, 2 prerequisites, 3 MinIO list/copy, 4 SQL, 5 confirm aborted.

set -euo pipefail
IFS=$'\n\t'

# ---------- defaults ----------
DATE=""
OWNER=""
TARGET_DB=""
LAYER="all"
DOMAIN_FILTER=""
DRY_RUN=0
FORCE=0
WORKDIR="/tmp/restore_$$"

S3_ALIAS="${S3_ALIAS:-local}"
S3_BUCKET="${S3_BUCKET:-l4-snapshots}"
S3_ENDPOINT="${S3_ENDPOINT:-}"
S3_ACCESS_KEY="${S3_ACCESS_KEY:-}"
S3_SECRET_KEY="${S3_SECRET_KEY:-}"

# ---------- helpers ----------
log()  { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
warn() { printf '[%s] WARN: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
die()  { printf '[%s] ERROR: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; exit "${2:-1}"; }

usage() {
    sed -n '2,20p' "$0"
    exit 1
}

cleanup() {
    local rc=$?
    if [[ -d "$WORKDIR" ]]; then
        rm -rf "$WORKDIR" || true
    fi
    exit "$rc"
}
trap cleanup EXIT INT TERM

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --date)       DATE="$2"; shift 2 ;;
        --owner)      OWNER="$2"; shift 2 ;;
        --target-db)  TARGET_DB="$2"; shift 2 ;;
        --layer)      LAYER="$2"; shift 2 ;;
        --domain)     DOMAIN_FILTER="$2"; shift 2 ;;
        --dry-run)    DRY_RUN=1; shift ;;
        --force)      FORCE=1; shift ;;
        -h|--help)    usage ;;
        *)            die "Unknown arg: $1" 1 ;;
    esac
done

# ---------- validate ----------
[[ -n "$DATE"      ]] || die "--date is required (YYYY-MM-DD)" 1
[[ -n "$OWNER"     ]] || die "--owner is required (UUID)" 1
[[ -n "$TARGET_DB" ]] || die "--target-db is required (postgres://...)" 1

[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "--date must be YYYY-MM-DD, got: $DATE" 1
[[ "$OWNER" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] \
    || die "--owner must be UUID, got: $OWNER" 1

case "$LAYER" in
    l1|l2|l3|all) ;;
    *) die "--layer must be l1|l2|l3|all, got: $LAYER" 1 ;;
esac

# L1/L2 не в L4 — предупреждаем
if [[ "$LAYER" == "l1" || "$LAYER" == "l2" ]]; then
    die "Layer $LAYER not present in L4 snapshots — use pg_dump backups instead (see docs)" 1
fi
if [[ "$LAYER" == "all" ]]; then
    warn "Layer 'all' selected — L4 only contains L3 data; L1/L2 will NOT be restored from snapshots."
fi

# ---------- prerequisites ----------
S3_TOOL=""
if command -v mc >/dev/null 2>&1; then
    S3_TOOL="mc"
elif command -v aws >/dev/null 2>&1; then
    S3_TOOL="aws"
    [[ -n "$S3_ENDPOINT" ]] || die "aws-cli fallback needs S3_ENDPOINT env var" 2
else
    die "Neither 'mc' nor 'aws' CLI found in PATH" 2
fi
command -v psql >/dev/null 2>&1 || die "psql not found in PATH" 2
command -v jq   >/dev/null 2>&1 || die "jq not found in PATH (parse JSON snapshots)" 2

log "Using S3 client: $S3_TOOL"

# Test target DB connectivity
if ! psql "$TARGET_DB" -c "SELECT 1" >/dev/null 2>&1; then
    die "Cannot connect to target DB" 2
fi
log "Target DB reachable."

# Setup mc alias if needed (idempotent)
if [[ "$S3_TOOL" == "mc" && -n "$S3_ENDPOINT" ]]; then
    mc alias set "$S3_ALIAS" "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY" --quiet >/dev/null 2>&1 || \
        warn "mc alias setup returned non-zero — assuming alias already exists"
fi

# ---------- pre-check: existing data ----------
EXISTING=$(psql "$TARGET_DB" -tA -c \
    "SELECT COUNT(*) FROM l3_master_knowledge WHERE owner_user_id = '$OWNER'")
EXISTING_TOOLS=$(psql "$TARGET_DB" -tA -c \
    "SELECT COUNT(*) FROM l3_tools_registry WHERE owner_user_id = '$OWNER'")

if [[ "${EXISTING:-0}" -gt 0 || "${EXISTING_TOOLS:-0}" -gt 0 ]]; then
    warn "Target already has data for owner $OWNER: l3_master_knowledge=$EXISTING, l3_tools_registry=$EXISTING_TOOLS"
    warn "Restore is idempotent (ON CONFLICT DO NOTHING) — existing rows kept, missing rows added."
    if [[ "$FORCE" -ne 1 ]]; then
        die "Use --force to confirm restore over existing data" 5
    fi
fi

# ---------- list snapshots ----------
mkdir -p "$WORKDIR"
SNAPS_FILE="$WORKDIR/snapshots.txt"

# Префикс per-owner (новый формат). Legacy без owner — отдельный путь, обычно не нужно.
S3_PREFIX="l4/$OWNER/"
log "Listing snapshots under s3://$S3_BUCKET/$S3_PREFIX ..."

if [[ "$S3_TOOL" == "mc" ]]; then
    # mc ls --recursive выводит per-object строки с датой; фильтруем по DATE
    mc ls --recursive "$S3_ALIAS/$S3_BUCKET/$S3_PREFIX" 2>/dev/null \
        | awk -v d="$DATE" '$0 ~ d {print $NF}' \
        > "$SNAPS_FILE" || die "mc ls failed" 3
else
    aws --endpoint-url "$S3_ENDPOINT" s3 ls "s3://$S3_BUCKET/$S3_PREFIX" --recursive 2>/dev/null \
        | awk -v d="$DATE" '$1 == d {print $NF}' \
        > "$SNAPS_FILE" || die "aws s3 ls failed" 3
fi

if [[ -n "$DOMAIN_FILTER" ]]; then
    grep "/$DOMAIN_FILTER/" "$SNAPS_FILE" > "$SNAPS_FILE.tmp" || true
    mv "$SNAPS_FILE.tmp" "$SNAPS_FILE"
fi

COUNT=$(wc -l < "$SNAPS_FILE" | tr -d ' ')
if [[ "$COUNT" -eq 0 ]]; then
    die "No snapshots found for owner=$OWNER date=$DATE${DOMAIN_FILTER:+ domain=$DOMAIN_FILTER}" 3
fi
log "Found $COUNT snapshot(s) to process."

if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN — snapshots that would be restored:"
    sed 's/^/  - /' "$SNAPS_FILE"
    exit 0
fi

# ---------- download + restore ----------
INSERTED_KNOWLEDGE=0
INSERTED_TOOLS=0
SKIPPED_FILES=0

while IFS= read -r key; do
    [[ -z "$key" ]] && continue
    local_file="$WORKDIR/$(basename "$key")"

    log "Downloading $key ..."
    if [[ "$S3_TOOL" == "mc" ]]; then
        # mc уже работает с alias; объект может прийти с префиксом bucket — нормализуем
        src="$S3_ALIAS/$S3_BUCKET/${key#"$S3_BUCKET/"}"
        mc cp --quiet "$src" "$local_file" >/dev/null 2>&1 \
            || { warn "Failed to download $key — skipping"; SKIPPED_FILES=$((SKIPPED_FILES+1)); continue; }
    else
        aws --endpoint-url "$S3_ENDPOINT" s3 cp "s3://$S3_BUCKET/$key" "$local_file" --quiet \
            || { warn "Failed to download $key — skipping"; SKIPPED_FILES=$((SKIPPED_FILES+1)); continue; }
    fi

    # Парсим JSON и заливаем
    if ! jq -e '.knowledge and .tools' "$local_file" >/dev/null 2>&1; then
        warn "Snapshot $key missing .knowledge or .tools — skipping"
        SKIPPED_FILES=$((SKIPPED_FILES+1))
        continue
    fi

    # KNOWLEDGE: каждый объект → INSERT ON CONFLICT (id) DO NOTHING
    KN_SQL="$WORKDIR/knowledge.sql"
    jq -r --arg owner "$OWNER" '
        .knowledge[] | @json' "$local_file" \
        | while IFS= read -r row; do
            # Используем psql parameterised через temp file + COPY would require flat TSV;
            # для надёжности jsonb используем jsonb_populate_record approach.
            : >> "$KN_SQL"
            id=$(printf '%s' "$row" | jq -r '.id')
            domain=$(printf '%s' "$row" | jq -r '.domain')
            ktype=$(printf '%s' "$row" | jq -r '.knowledge_type')
            content=$(printf '%s' "$row" | jq -c '.content')
            version=$(printf '%s' "$row" | jq -r '.version // 1')
            eff_from=$(printf '%s' "$row" | jq -r '.effective_from // empty')
            content_escaped=${content//\'/\'\'}
            cat >> "$KN_SQL" <<SQL
INSERT INTO l3_master_knowledge
    (id, owner_user_id, domain, knowledge_type, content, version, effective_from)
VALUES ('$id', '$OWNER', '$domain', '$ktype', '$content_escaped'::jsonb, $version,
        ${eff_from:+'$eff_from'::timestamptz}${eff_from:-NOW()})
ON CONFLICT (id) DO NOTHING;
SQL
        done

    # TOOLS
    TL_SQL="$WORKDIR/tools.sql"
    jq -r '.tools[] | @json' "$local_file" \
        | while IFS= read -r row; do
            : >> "$TL_SQL"
            id=$(printf '%s' "$row" | jq -r '.id')
            domain=$(printf '%s' "$row" | jq -r '.domain')
            tname=$(printf '%s' "$row" | jq -r '.tool_name')
            ttype=$(printf '%s' "$row" | jq -r '.tool_type // "service"')
            desc=$(printf '%s' "$row" | jq -r '.description // ""')
            cfg=$(printf '%s' "$row" | jq -c '.config_schema // {}')
            up=$(printf '%s' "$row" | jq -c '.usage_patterns // {}')
            desc_esc=${desc//\'/\'\'}
            cfg_esc=${cfg//\'/\'\'}
            up_esc=${up//\'/\'\'}
            cat >> "$TL_SQL" <<SQL
INSERT INTO l3_tools_registry
    (id, owner_user_id, domain, tool_name, tool_type, description,
     config_schema, usage_patterns, version, effective_from)
VALUES ('$id', '$OWNER', '$domain', '$tname', '$ttype', '$desc_esc',
        '$cfg_esc'::jsonb, '$up_esc'::jsonb, 1, NOW())
ON CONFLICT (id) DO NOTHING;
SQL
        done

    # Выполняем единой транзакцией per snapshot
    if [[ -s "$KN_SQL" ]]; then
        BEFORE=$(psql "$TARGET_DB" -tA -c "SELECT COUNT(*) FROM l3_master_knowledge")
        psql "$TARGET_DB" --single-transaction -v ON_ERROR_STOP=1 -f "$KN_SQL" >/dev/null \
            || die "psql failed loading knowledge from $key" 4
        AFTER=$(psql "$TARGET_DB" -tA -c "SELECT COUNT(*) FROM l3_master_knowledge")
        INSERTED_KNOWLEDGE=$((INSERTED_KNOWLEDGE + AFTER - BEFORE))
    fi
    if [[ -s "$TL_SQL" ]]; then
        BEFORE=$(psql "$TARGET_DB" -tA -c "SELECT COUNT(*) FROM l3_tools_registry")
        psql "$TARGET_DB" --single-transaction -v ON_ERROR_STOP=1 -f "$TL_SQL" >/dev/null \
            || die "psql failed loading tools from $key" 4
        AFTER=$(psql "$TARGET_DB" -tA -c "SELECT COUNT(*) FROM l3_tools_registry")
        INSERTED_TOOLS=$((INSERTED_TOOLS + AFTER - BEFORE))
    fi
    rm -f "$KN_SQL" "$TL_SQL"
done < "$SNAPS_FILE"

# ---------- summary + verification ----------
log "============================================================"
log "RESTORE SUMMARY"
log "  Owner:               $OWNER"
log "  Snapshots processed: $((COUNT - SKIPPED_FILES))/$COUNT"
log "  L3 knowledge added:  $INSERTED_KNOWLEDGE"
log "  L3 tools added:      $INSERTED_TOOLS"
log "  Skipped files:       $SKIPPED_FILES"
log "============================================================"
log "Verification queries:"
log "  psql \"$TARGET_DB\" -c \"SELECT domain, COUNT(*) FROM l3_master_knowledge"
log "    WHERE owner_user_id='$OWNER' GROUP BY domain ORDER BY 1;\""
log "  psql \"$TARGET_DB\" -c \"SELECT domain, COUNT(*) FROM l3_tools_registry"
log "    WHERE owner_user_id='$OWNER' GROUP BY domain ORDER BY 1;\""

log "Done. Vectors (HNSW index) will need re-population: POST /admin/reindex-vectors?owner=$OWNER"
exit 0
