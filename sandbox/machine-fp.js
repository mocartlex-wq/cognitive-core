// Stable per-browser machine fingerprint for /user/agents/create
// and /user/connect/auto-onboard. Persistent в localStorage до явной очистки
// данных браузера. Цель — не плодить «новые машины» при каждом логине из
// одного устройства.
//
// Usage:
//   const fp = await getMachineFingerprint();    // "92c8f62a55bfb518"
//   const label = getMachineLabel();              // "YaBrowser on Windows"
//
// Stability: navigator.userAgent + language + screen + timezone + platform.
// Эта комбинация устойчива к reboot, обновлениям ОС, но меняется при:
//   - очистке localStorage
//   - смене разрешения экрана / профиля браузера
//   - переезде в другой браузер
// Это приемлемо — пользователь явно меняет среду.
window.getMachineFingerprint = async function () {
  const STORAGE_KEY = 'cogcore_machine_fp_v1';
  const cached = localStorage.getItem(STORAGE_KEY);
  if (cached && /^[a-f0-9]{16}$/.test(cached)) return cached;
  const data = [
    navigator.userAgent || '',
    navigator.language || '',
    (screen.width || 0) + 'x' + (screen.height || 0),
    String(new Date().getTimezoneOffset()),
    navigator.platform || '',
    (navigator.hardwareConcurrency || 0) + 'c',
  ].join('|');
  const buf = new TextEncoder().encode(data);
  const hash = await crypto.subtle.digest('SHA-256', buf);
  const fp = Array.from(new Uint8Array(hash))
    .map(b => b.toString(16).padStart(2, '0'))
    .join('')
    .slice(0, 16);
  localStorage.setItem(STORAGE_KEY, fp);
  return fp;
};

window.getMachineLabel = function () {
  const ua = navigator.userAgent || '';
  let browser = 'Browser';
  const m = ua.match(/(YaBrowser|Edg|Chrome|Firefox|Safari|Opera)\/?[\d.]*/);
  if (m) browser = m[1] === 'Edg' ? 'Edge' : m[1];
  let os = navigator.platform || 'unknown';
  if (/Win/i.test(os)) os = 'Windows';
  else if (/Mac/i.test(os)) os = 'macOS';
  else if (/Linux/i.test(os)) os = 'Linux';
  return browser + ' on ' + os;
};
