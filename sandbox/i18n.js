// Lightweight i18n: data-i18n attributes + JSON bundles + langSelect
// Usage:
//   <span data-i18n="home.title">Запомнить всё</span>
//   <input data-i18n="login.email_label" data-i18n-attr="placeholder">
// On page load: replaces text (or attribute) with translation for current locale.
// Destination: sandbox/i18n.js — served from /static/i18n.js
// Bundles served from /static/locales/{ru,en}.json

(function () {
  const STORAGE_KEY = 'cogcore_lang';
  const DEFAULT_LANG = 'ru';
  const SUPPORTED = ['ru', 'en'];

  function detectLang() {
    // 1. URL param ?lang=en  (highest priority — shareable links)
    const url = new URLSearchParams(location.search).get('lang');
    if (url && SUPPORTED.includes(url)) return url;
    // 2. localStorage preference (user explicit choice)
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && SUPPORTED.includes(stored)) return stored;
    // 3. browser navigator.language (first-visit auto)
    const browser = (navigator.language || '').slice(0, 2).toLowerCase();
    if (SUPPORTED.includes(browser)) return browser;
    return DEFAULT_LANG;
  }

  async function loadBundle(lang) {
    try {
      const r = await fetch(`/static/locales/${lang}.json`, { cache: 'force-cache' });
      return r.ok ? await r.json() : {};
    } catch (e) {
      console.warn('[i18n] bundle load failed', lang, e);
      return {};
    }
  }

  function applyTranslations(bundle) {
    document.querySelectorAll('[data-i18n]').forEach((el) => {
      const key = el.dataset.i18n;
      const text = key.split('.').reduce((o, k) => (o == null ? o : o[k]), bundle);
      if (text == null) return;
      if (el.dataset.i18nAttr) {
        // Translate an attribute (placeholder, title, aria-label, alt, value, ...)
        el.setAttribute(el.dataset.i18nAttr, text);
      } else {
        el.textContent = text;
      }
    });
  }

  async function init() {
    const lang = detectLang();
    document.documentElement.lang = lang;
    document.documentElement.dataset.lang = lang;
    const bundle = await loadBundle(lang);
    window.cogcore_i18n = { lang, bundle };
    applyTranslations(bundle);

    // Global lang switcher — call from any button: onclick="cogcore_setLang('en')"
    window.cogcore_setLang = function (newLang) {
      if (!SUPPORTED.includes(newLang)) return;
      localStorage.setItem(STORAGE_KEY, newLang);
      location.reload();
    };

    // Convenience: t('home.title') for programmatic strings
    window.cogcore_t = function (key, fallback) {
      const v = key.split('.').reduce((o, k) => (o == null ? o : o[k]), bundle);
      return v == null ? (fallback != null ? fallback : key) : v;
    };

    document.dispatchEvent(new CustomEvent('cogcore:i18n-ready', { detail: { lang } }));
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
