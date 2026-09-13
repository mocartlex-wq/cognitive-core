"""Зависимости закреплены, и закрепление не расходится с намерением.

13.09.2026. Соседний контур поймал на своей сборке: PyJWT 2.13→2.14 и
alembic 1.19→1.20 приехали сами, без единой правки pyproject. У нас было то
же самое — ни одна из 28 прямых зависимостей не была закреплена точно
(openai==1.*, fastapi==0.115.*), просто ещё не выстрелило. Выкат, в котором
не менялось ни строчки кода, мог прийти с новой минорной версией, и такую
поломку ищут дольше всего: диф пуст, а прод лёг.

Лечение — файл ограничений, снятый с работающего прода. Этот тест держит его
честным в обе стороны:
  - прямая зависимость без точной версии в lock снова плавает;
  - версия в lock вне диапазона requirements.txt — сборка упадёт на конфликте,
    и лучше узнать это в CI, чем на проде;
  - Dockerfile, не использующий lock, делает весь файл декоративным.

Последний пункт не формальность: сторож, который выглядит установленным и не
подключён, за эти две недели встречался чаще любого другого дефекта.
"""
from __future__ import annotations

import pathlib
import re

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = pathlib.Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock.txt"
DOCKERFILE = ROOT / "Dockerfile"


def _lines(path: pathlib.Path) -> list[str]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _direct() -> list[Requirement]:
    return [Requirement(line) for line in _lines(REQ)]


def _locked() -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for line in _lines(LOCK):
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;]+)", line)
        assert m, f"в lock строка не вида пакет==версия: {line!r}"
        pins[canonicalize_name(m.group(1))] = Version(m.group(2))
    return pins


def test_lock_exists_and_is_not_empty():
    assert LOCK.exists(), "файла ограничений нет — версии снова плавают"
    assert len(_locked()) >= len(_direct())


@pytest.mark.parametrize("req", _direct(), ids=lambda r: r.name)
def test_every_direct_dependency_is_pinned(req: Requirement):
    """Прямая зависимость без точной версии — ровно исходный дефект."""
    assert canonicalize_name(req.name) in _locked(), (
        f"{req.name} есть в requirements.txt, но не закреплён в lock — "
        "сборка возьмёт то, что вышло к её часу"
    )


@pytest.mark.parametrize("req", _direct(), ids=lambda r: r.name)
def test_pin_satisfies_declared_range(req: Requirement):
    """Закреплённая версия обязана попадать в диапазон намерения.

    Поднял fastapi до 0.116.* в requirements.txt и забыл переснять lock —
    pip упадёт на конфликте при сборке. Пусть лучше покраснеет здесь.
    """
    pinned = _locked()[canonicalize_name(req.name)]
    assert req.specifier.contains(pinned, prereleases=True), (
        f"{req.name}: в lock {pinned}, а requirements.txt требует {req.specifier}"
    )


def test_dockerfile_installs_with_constraints():
    """Lock, который сборка не читает, — декорация."""
    src = DOCKERFILE.read_text(encoding="utf-8")
    # Ищем именно строку COPY. Первая версия проверяла имя файла где угодно и
    # оставалась зелёной, когда lock убрали из COPY: имя всё равно встречалось
    # в строке pip install. «Упомянут» и «скопирован в образ» — разное, и
    # сборка на втором упадёт с «файл не найден». Поймано подменой.
    copies = [ln for ln in src.splitlines() if ln.strip().startswith("COPY")]
    assert any("requirements.lock.txt" in ln for ln in copies), (
        "Dockerfile не копирует lock в образ — pip не найдёт файл ограничений"
    )
    install = [ln for ln in src.splitlines() if "pip install" in ln and "requirements.txt" in ln]
    assert install, "не найдена строка установки зависимостей"
    assert any("-c requirements.lock.txt" in ln for ln in install), (
        "pip install идёт без -c requirements.lock.txt — версии снова разрешаются заново"
    )


def test_dependencies_are_installed_before_code():
    """Слой зависимостей до COPY . . — иначе правка кода пересобирает все колёса.

    Отдельный пункт соседей 13.09: у них полная сборка держала очередь
    выкатов больше получаса. У нас порядок верный — фиксируем, чтобы не сломать.
    """
    src = DOCKERFILE.read_text(encoding="utf-8")
    i_install = src.find("pip install")
    i_code = src.find("COPY . .")
    assert 0 <= i_install < i_code, "зависимости ставятся после копирования кода"
