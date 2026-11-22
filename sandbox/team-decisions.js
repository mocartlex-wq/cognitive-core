(() => {
  const el = id => document.getElementById(id);
  const id = new URLSearchParams(location.search).get('conflict');
  const base = '/user/team-decisions';
  let detail, feedbackId;
  el('retry').onclick = () => location.reload();
  async function request(path, body) {
    const options = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {};
    const response = await fetch(path, options);
    if (response.status === 401) el('login').hidden = false;
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw Error(typeof error.detail === 'string' ? error.detail : 'Не удалось выполнить запрос.');
    }
    return response.json();
  }
  function item(list, text) {
    const li = document.createElement('li');
    li.textContent = text;
    list.append(li);
    return li;
  }
  function display(data) {
    detail = data;
    el('detail').hidden = false;
    el('title').textContent = data.case.title;
    el('scope').textContent = [data.case.project, data.case.team, data.case.role, ...data.case.conditions].join(' · ');
    el('instance').textContent = 'Просмотр ' + data.instance_id + ' · ' + data.opened_by;
    el('rules').replaceChildren();
    el('rule-select').replaceChildren();
    for (const rule of data.assessment.rules) {
      const label = rule.address + ' · ' + rule.reason + ' · вес ' + rule.weight;
      const active = data.assessment.applied_rule_ids.includes(rule.rule_id);
      item(el('rules'), label + ' · ' + data.options[rule.choice] + (active ? '' : ' (уточнено или ниже по приоритету)'));
      if (data.assessment.applied_rule_ids.includes(rule.rule_id)) el('rule-select').add(new Option(label,rule.rule_id));
    }
    const duration = {future:'Для будущих просмотров в этой области.',once:'Только для этого просмотра.',
                      inherited:'По ранее подтверждённому решению владельца.',rules:'По применимым правилам.'};
    el('outcome').textContent = data.choice ? data.options[data.choice] + '. ' +
      (data.revoked_at ? 'Решение на будущее отозвано.' : duration[data.duration]) : 'Два правила задают разный порядок. Выберите вариант и срок.';
    el('vote').hidden = !!data.choice;
    el('feedback').hidden = !data.choice;
    el('revoke').hidden = data.duration !== 'future' || !!data.revoked_at;
  }
  async function emit(button, action) {
    button.disabled = true;
    try { await action(); }
    catch (error) { el('status').textContent = error.message; }
    finally { button.disabled = false; }
  }
  el('vote').onsubmit = event => {
    event.preventDefault();
    emit(event.submitter, async () => {
      display(await request(base+'/'+id+'/answer', Object.fromEntries(new FormData(el('vote')))));
      el('status').textContent = 'Ваш выбор сохранён.';
    });
  };
  el('revoke').onclick = event => emit(event.currentTarget, async () => {
    display(await request(base+'/'+id+'/revoke', {}));
    el('status').textContent = 'Новые просмотры снова проверят правила.';
  });
  el('rate').oninput = () => { feedbackId = null; };
  el('rate').onsubmit = event => {
    event.preventDefault();
    emit(event.submitter, async () => {
      const data = Object.fromEntries(new FormData(el('rate')));
      feedbackId ||= crypto.randomUUID();
      const receipt = await request(base+'/cases/'+detail.case.case_id+'/feedback',
        {...data, delta:Number(data.delta), id:feedbackId, evidence_run_id:id});
      el('status').textContent = 'Вес ' + receipt.old_weight + ' → ' + receipt.new_weight + '. ' + receipt.reason;
    });
  };
  async function load() {
    if (id) {
      display(await request(base+'/'+encodeURIComponent(id)));
    } else {
      const {items} = await request(base);
      el('recent').hidden = false;
      const list = el('recent').querySelector('ul');
      if (!items.length) item(list,'Пока нет просмотров.');
      for (const row of items) {
        const link = document.createElement('a');
        link.href = '?conflict='+encodeURIComponent(row.id);
        link.textContent = row.title + ' · ' + new Date(row.created_at).toLocaleString('ru-RU') +
          (row.choice ? ' · есть решение' : ' · ждёт выбора');
        item(list,'').append(link);
      }
    }
    el('status').textContent = '';
  }
  load().catch(error => { el('status').textContent = error.message; });
})();
