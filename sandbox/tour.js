/* Onboarding tour для главной страницы.
   Использует Shepherd.js (CDN). Запускается:
   - автоматически при первом визите (флаг localStorage cc_tour_done)
   - вручную при клике на ".start-tour" */

(function() {
  if (typeof Shepherd === 'undefined') return;

  const STORAGE_KEY = 'cc_tour_done_v1';

  function buildTour() {
    const tour = new Shepherd.Tour({
      useModalOverlay: true,
      defaultStepOptions: {
        cancelIcon: { enabled: true },
        scrollTo: { behavior: 'smooth', block: 'center' },
        classes: 'cc-step',
        modalOverlayOpeningPadding: 6,
        modalOverlayOpeningRadius: 8,
      },
    });

    tour.addStep({
      id: 'intro',
      title: 'Cognitive Core — короткая экскурсия',
      text: `<p>За 1 минуту покажу как устроена 5-слойная память для AI-агентов.</p>
             <p>Можно прервать в любой момент — крестик справа сверху.</p>`,
      buttons: [
        { text: 'Пропустить', secondary: true, action: tour.cancel },
        { text: 'Начать', action: tour.next },
      ],
    });

    tour.addStep({
      id: 'flow',
      title: 'Идея в одной картинке',
      attachTo: { element: '.flow', on: 'top' },
      text: `<p>Данные движутся слева направо: <strong>сырые события (L1) → дневные срезы (L2) → выученные знания (L3) → бэкапы (L4)</strong>.</p>
             <p>L5 — журнал аудита всех действий.</p>
             <p>Числа в карточках — реальное состояние памяти прямо сейчас.</p>`,
      buttons: [
        { text: 'Назад', secondary: true, action: tour.back },
        { text: 'Дальше', action: tour.next },
      ],
    });

    tour.addStep({
      id: 'channels',
      title: 'Две точки взаимодействия',
      attachTo: { element: '.channels', on: 'top' },
      text: `<p>Агент НЕ читает и НЕ пишет в L1-L4 напрямую. Только два канала:</p>
             <ul>
               <li><strong>POST /events</strong> — записать опыт</li>
               <li><strong>POST /operative/query</strong> — найти релевантные знания</li>
             </ul>
             <p>Это и есть «мембрана» защиты — никаких прямых SQL-запросов.</p>`,
      buttons: [
        { text: 'Назад', secondary: true, action: tour.back },
        { text: 'Дальше', action: tour.next },
      ],
    });

    tour.addStep({
      id: 'lifecycle',
      title: 'Что происходит с одним событием',
      attachTo: { element: '.lifecycle', on: 'top' },
      text: `<p>5 шагов от «agent сделал что-то» до «знание используется при поиске».</p>
             <p>Обычный путь — <strong>7-8 дней</strong> (день в L1 → daily в L2 → 7 дней накопления → weekly в L3).</p>
             <p>Куратор-LLM фильтрует шум на каждом переходе.</p>`,
      buttons: [
        { text: 'Назад', secondary: true, action: tour.back },
        { text: 'Дальше', action: tour.next },
      ],
    });

    tour.addStep({
      id: 'quick',
      title: 'Три способа начать',
      attachTo: { element: '.quick', on: 'top' },
      text: `<p>Кнопка <strong>«Запустить полный цикл»</strong> — всё происходит за 1 минуту:
             18 событий → daily через DeepSeek → weekly через DeepSeek → KNN-поиск.</p>
             <p>После этого числа в карточках слоёв вырастут — увидите как L1 превращается в L3.</p>`,
      buttons: [
        { text: 'Назад', secondary: true, action: tour.back },
        { text: 'Дальше', action: tour.next },
      ],
    });

    tour.addStep({
      id: 'nav',
      title: 'Навигация',
      attachTo: { element: '.top-nav', on: 'bottom' },
      text: `<p><strong>Дашборд</strong> — live-метрики, обозреватель слоёв и аудита.</p>
             <p><strong>Песочница API</strong> — все 13 эндпоинтов сгруппированы по 5 этапам жизненного цикла.</p>
             <p>Тур всегда можно перезапустить — кнопка <strong>?</strong> в шапке.</p>`,
      buttons: [
        { text: 'Назад', secondary: true, action: tour.back },
        { text: 'Готово', action: () => { tour.complete(); } },
      ],
    });

    tour.on('complete', () => localStorage.setItem(STORAGE_KEY, '1'));
    tour.on('cancel',   () => localStorage.setItem(STORAGE_KEY, '1'));

    return tour;
  }

  // Public API
  window.startCognitiveTour = function() {
    const tour = buildTour();
    tour.start();
  };

  // Auto-start on first visit
  document.addEventListener('DOMContentLoaded', () => {
    if (!localStorage.getItem(STORAGE_KEY)) {
      // Маленькая задержка чтобы health/stats успели загрузиться
      setTimeout(() => window.startCognitiveTour(), 1200);
    }
  });
})();
