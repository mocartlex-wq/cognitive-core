"""Уборщик docker не выметает общий кэш сборки и не врёт о его размере.

13.09.2026. Соседний контур собирал прод больше часа и искал причину в
отсутствии политики GC для кэша сборщика. Политика была — самая агрессивная
из возможных, и она была нашей: `cogcore-docker-prune.timer` каждый день в
04:30 выполнял `docker builder prune -af`, то есть удалял ВЕСЬ кэш, не занятый
в эту секунду. Кэш на машине общий, поэтому любая сборка после 04:30 — наша
или соседская — шла с холодного кэша и заново качала все колёса.

Хуже: журнал уборщика каждый день писал `build_cache=0`, пока кэш весил
23 ГБ. Размер читался `awk '/Build Cache/ {print $4}'`, но название типа —
два слова, и все колонки в этой строке сдвинуты на одну: $4 оказывался числом
активных записей. Этот ноль и меня самого сначала убедил, что кэш «уже пуст».
Проверено на живом сервере: старый способ даёт 0, `--format` даёт 23.23GB.

Замер, который лжёт, опаснее отсутствия замера: он не просто молчит, он
отвечает на вопрос неверно и уверенно.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "docker-prune-weekly.sh"


def _code() -> str:
    """Текст без строк-комментариев: в них честно описано старое поведение."""
    return "\n".join(ln for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#"))


def test_build_cache_is_not_wiped_entirely():
    code = _code()
    # Только настоящий вызов docker. Первая версия брала любую строку со
    # словами «builder prune» и краснела на строке журнала
    # `log "builder prune: $RECLAIMED_CACHE"`, где флагов потолка быть не может.
    # Поймано тем, что тест упал на уже исправленном скрипте, до всякой подмены.
    prune = [ln for ln in code.splitlines() if "docker builder prune" in ln]
    assert prune, "уборка кэша сборки пропала вовсе — диск будет расти без предела"
    for ln in prune:
        assert not re.search(r"builder prune\s+-\w*a", ln), (
            f"builder prune с -a: выметает и внутренние образы фронтенда сборки: {ln.strip()}"
        )
        assert "--max-used-space" in ln or "--reserved-space" in ln, (
            "builder prune без потолка удаляет весь незанятый кэш — "
            f"любая сборка после уборки идёт с холодного кэша: {ln.strip()}"
        )


def test_sizes_are_read_with_format_not_columns():
    """Колонки вывода для человека сдвигаются от числа слов в названии типа."""
    code = _code()
    assert not re.search(r"awk\s+'/Build Cache/\s*\{print \$\d\}'", code), (
        "размер кэша снова читается колонкой: у «Build Cache» два слова, "
        "$4 там — число активных записей, а не размер"
    )
    assert "docker system df --format" in code


def test_cap_is_configurable():
    """Потолок — параметр, а не число в середине команды."""
    assert re.search(r'BUILD_CACHE_CAP="\$\{BUILD_CACHE_CAP:-\d+GB\}"', _code())
