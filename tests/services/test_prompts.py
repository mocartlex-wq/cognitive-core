"""Unit tests для app/services/prompts.py (M1-extension, coverage goal 70%).

Pure string/dict logic — no IO, no DB, no network. Единственная зависимость —
settings.system_language, патчится через unittest.mock.patch.

Module under test: мультиязычные промпты + getter'ы с fallback на "ru".
Главный инвариант: каждый языковой вариант каждого промпта должен принимать
РОВНО те .format()-плейсхолдеры, что подставляются в реальных call-site'ах
(analyzer.py / curator.py). Лишний/недостающий {placeholder} уронил бы
non-default язык в рантайме — этот файл ловит такие регрессии.
"""
from unittest.mock import patch

from app.services.prompts import (
    AUDIT_PROMPTS,
    DAILY_PROMPTS,
    FILTER_PROMPTS,
    QUALITY_PROMPTS,
    SUPPORTED_LANGUAGES,
    USER_DAILY,
    USER_RETRY,
    USER_WEEKLY,
    WEEKLY_PROMPTS,
    get_audit_prompt,
    get_daily_prompt,
    get_filter_prompt,
    get_quality_prompt,
    get_user_daily,
    get_user_retry,
    get_user_weekly,
    get_weekly_prompt,
    lang,
)

# .format() kwargs ровно как в реальных call-site'ах analyzer.py / curator.py
DAILY_KWARGS = {"domain": "design"}
WEEKLY_KWARGS = {
    "domain": "design",
    "current_l3": "[]",
    "current_tools": "[]",
    "weekly_buffers": "[]",
}
FILTER_KWARGS = {"domain": "design", "min_events": 5}
QUALITY_KWARGS = {"domain": "design", "min_repetitions": 2, "min_confidence": 0.7}
AUDIT_KWARGS = {
    "domain": "design",
    "now": "2026-05-28",
    "staleness_days": 90,
    "unused_days": 30,
}
USER_DAILY_KWARGS = {"events_json": "[]"}


class TestLang:
    def test_default_is_ru_when_missing(self):
        # getattr fallback: атрибут отсутствует → "ru"
        with patch("app.services.prompts.settings") as s:
            del s.system_language
            assert lang() == "ru"

    def test_returns_supported_code(self):
        with patch("app.services.prompts.settings") as s:
            s.system_language = "en"
            assert lang() == "en"

    def test_unsupported_code_falls_back_to_ru(self):
        with patch("app.services.prompts.settings") as s:
            s.system_language = "de"  # не в SUPPORTED_LANGUAGES
            assert lang() == "ru"

    def test_empty_string_falls_back_to_ru(self):
        # `code or "ru"` ветка для falsy значения
        with patch("app.services.prompts.settings") as s:
            s.system_language = ""
            assert lang() == "ru"

    def test_none_falls_back_to_ru(self):
        with patch("app.services.prompts.settings") as s:
            s.system_language = None
            assert lang() == "ru"

    def test_every_supported_code_passes_through(self):
        for code in SUPPORTED_LANGUAGES:
            with patch("app.services.prompts.settings") as s:
                s.system_language = code
                assert lang() == code


class TestGettersExplicitLanguage:
    def test_daily_returns_requested_language(self):
        assert get_daily_prompt("en") == DAILY_PROMPTS["en"]
        assert get_daily_prompt("ru") == DAILY_PROMPTS["ru"]

    def test_weekly_returns_requested_language(self):
        assert get_weekly_prompt("ja") == WEEKLY_PROMPTS["ja"]

    def test_filter_returns_requested_language(self):
        assert get_filter_prompt("es") == FILTER_PROMPTS["es"]

    def test_quality_returns_requested_language(self):
        assert get_quality_prompt("ko") == QUALITY_PROMPTS["ko"]

    def test_audit_returns_requested_language(self):
        assert get_audit_prompt("pt") == AUDIT_PROMPTS["pt"]

    def test_user_getters_return_requested_language(self):
        assert get_user_daily("en") == USER_DAILY["en"]
        assert get_user_weekly("zh") == USER_WEEKLY["zh"]
        assert get_user_retry("ar") == USER_RETRY["ar"]


class TestGettersUnknownLanguageFallback:
    def test_unknown_language_falls_back_to_ru(self):
        # .get(key, default["ru"]) — несуществующий ключ → русский вариант
        assert get_daily_prompt("xx") == DAILY_PROMPTS["ru"]
        assert get_weekly_prompt("xx") == WEEKLY_PROMPTS["ru"]
        assert get_filter_prompt("xx") == FILTER_PROMPTS["ru"]
        assert get_quality_prompt("xx") == QUALITY_PROMPTS["ru"]
        assert get_audit_prompt("xx") == AUDIT_PROMPTS["ru"]
        assert get_user_daily("xx") == USER_DAILY["ru"]
        assert get_user_weekly("xx") == USER_WEEKLY["ru"]
        assert get_user_retry("xx") == USER_RETRY["ru"]


class TestGettersImplicitLanguage:
    def test_none_uses_lang_default_ru(self):
        with patch("app.services.prompts.settings") as s:
            s.system_language = "ru"
            assert get_daily_prompt(None) == DAILY_PROMPTS["ru"]
            assert get_user_weekly(None) == USER_WEEKLY["ru"]

    def test_none_uses_lang_resolved_language(self):
        with patch("app.services.prompts.settings") as s:
            s.system_language = "en"
            assert get_daily_prompt() == DAILY_PROMPTS["en"]
            assert get_quality_prompt() == QUALITY_PROMPTS["en"]


class TestFormatPlaceholdersValid:
    """Каждый язык должен .format() без KeyError/IndexError на реальных kwargs."""

    def test_daily_format_all_languages(self):
        for code in SUPPORTED_LANGUAGES:
            out = DAILY_PROMPTS[code].format(**DAILY_KWARGS)
            assert "design" in out

    def test_weekly_format_all_languages(self):
        for code in SUPPORTED_LANGUAGES:
            out = WEEKLY_PROMPTS[code].format(**WEEKLY_KWARGS)
            assert "design" in out

    def test_filter_format_all_languages(self):
        for code in SUPPORTED_LANGUAGES:
            out = FILTER_PROMPTS[code].format(**FILTER_KWARGS)
            assert "design" in out

    def test_quality_format_all_languages(self):
        for code in SUPPORTED_LANGUAGES:
            # не должно бросить KeyError на min_repetitions/min_confidence
            QUALITY_PROMPTS[code].format(**QUALITY_KWARGS)

    def test_audit_format_all_languages(self):
        for code in SUPPORTED_LANGUAGES:
            out = AUDIT_PROMPTS[code].format(**AUDIT_KWARGS)
            assert "2026-05-28" in out

    def test_user_daily_format_all_languages(self):
        for code in SUPPORTED_LANGUAGES:
            USER_DAILY[code].format(**USER_DAILY_KWARGS)


class TestPromptDictsComplete:
    """Все промпт-словари должны покрывать каждый поддерживаемый язык."""

    def test_all_dicts_cover_supported_languages(self):
        for mapping in (
            DAILY_PROMPTS,
            WEEKLY_PROMPTS,
            FILTER_PROMPTS,
            QUALITY_PROMPTS,
            AUDIT_PROMPTS,
            USER_DAILY,
            USER_WEEKLY,
            USER_RETRY,
        ):
            missing = SUPPORTED_LANGUAGES - set(mapping)
            assert not missing, f"missing languages: {missing}"

    def test_ru_fallback_key_present_everywhere(self):
        for mapping in (
            DAILY_PROMPTS,
            WEEKLY_PROMPTS,
            FILTER_PROMPTS,
            QUALITY_PROMPTS,
            AUDIT_PROMPTS,
            USER_DAILY,
            USER_WEEKLY,
            USER_RETRY,
        ):
            assert "ru" in mapping
