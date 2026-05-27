#!/usr/bin/env bash
# sync-helpers.sh — sandbox/<->scripts/ drift detector.
#
# Background: некоторые helper-скрипты дублируются в двух местах:
#   scripts/<name>      — source of truth, в репо
#   sandbox/<name>      — копия, serv-ится через FastAPI StaticFiles mount /static/
#
# Список synced helpers:
#   cogmedia              — bash CLI для media upload
#   install-self-hosted.sh — installer для VPS
#
# Если они drift'нут — клиенты которые делают `curl /static/<X>` получают
# устаревшую версию. Этот скрипт детектит drift в CI (exit 1 если diff).
#
# Запуск:
#   ./scripts/sync-helpers.sh check   — exit 0 если синхронны, 1 если diff
#   ./scripts/sync-helpers.sh fix     — копирует scripts/* в sandbox/*

set -e

cd "$(dirname "$0")/.."  # repo root

HELPERS=(
    "cogmedia"
    "install-self-hosted.sh"
)

mode="${1:-check}"

declare -i diff_count=0

for helper in "${HELPERS[@]}"; do
    src="scripts/$helper"
    dst="sandbox/$helper"
    if [ ! -f "$src" ]; then
        echo "WARN: source missing: $src"
        continue
    fi
    if [ ! -f "$dst" ]; then
        echo "DIFF: $dst missing"
        diff_count+=1
        if [ "$mode" = "fix" ]; then
            cp "$src" "$dst"
            echo "  fixed: copied $src → $dst"
        fi
        continue
    fi
    if ! cmp -s "$src" "$dst"; then
        echo "DIFF: scripts/$helper != sandbox/$helper"
        diff_count+=1
        if [ "$mode" = "fix" ]; then
            cp "$src" "$dst"
            echo "  fixed: copied $src → $dst"
        fi
    fi
done

if [ "$mode" = "check" ] && [ "$diff_count" -gt 0 ]; then
    echo
    echo "❌ Found $diff_count drift(s). Run 'bash scripts/sync-helpers.sh fix' to resolve."
    exit 1
fi

if [ "$diff_count" -eq 0 ]; then
    echo "✓ All helpers in sync (scripts/ ↔ sandbox/)"
fi
