(() => {
  const el = id => document.getElementById(id);
  const id = new URLSearchParams(location.search).get('conflict');
  const base = '/user/team-decisions';
  let detail, feedbackId;
  function chatDraft(conflict) {
    const values = new URLSearchParams(location.hash.slice(1));
    const choice = values.get('choice'), duration = values.get('duration');
    if (values.size !== 3 || values.get('for') !== conflict ||
        !['diagram_first', 'table_first'].includes(choice) || !['once', 'future'].includes(duration)) return null;
    return {choice, duration};
  }
  el('retry').onclick = () => location.reload();
  async function request(path, body) {
    const options = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {};
    const response = await fetch(path, options);
    if (response.status === 401) el('login').hidden = false;
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      const failure = Error(typeof error.detail === 'string' ? error.detail : 'Не удалось выполнить запрос.');
      failure.status = response.status;
      throw failure;
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
    const draft = data.choice ? null : chatDraft(data.conflict_id);
    el('outcome').textContent = data.choice ? data.options[data.choice] + '. ' +
      (data.revoked_at ? 'Решение на будущее отозвано.' : duration[data.duration]) :
      'Ожидает подтверждения. Ответ в чате вступит в силу, когда вы подтвердите выбор здесь.';
    el('chat-draft').hidden = !draft;
    if (draft) el('draft-summary').textContent = data.options[draft.choice] + '. ' + duration[draft.duration];
    el('vote').hidden = !!data.choice || !!draft;
    el('feedback').hidden = !data.choice;
    el('revoke').hidden = data.duration !== 'future' || !!data.revoked_at;
  }
  async function emit(button, action) {
    button.disabled = true;
    try { await action(); }
    catch (error) { el('status').textContent = error.message; }
    finally { button.disabled = false; }
  }
  async function confirmAnswer(body) {
    try {
      display(await request(base+'/'+id+'/answer', body));
    } catch (error) {
      if (error.status !== 409) throw error;
      display(await request(base+'/'+id));
      throw error;
    }
    el('status').textContent = 'Ваш выбор сохранён. Вернитесь в исходный чат, чтобы продолжить этот просмотр.';
  }
  el('confirm-draft').onclick = event => emit(event.currentTarget, async () => {
    const draft = chatDraft(detail.conflict_id);
    if (draft && !detail.choice) await confirmAnswer(draft);
    else display(detail);
  });
  el('edit-draft').onclick = () => {
    history.replaceState(null, '', location.pathname + location.search);
    display(detail);
    el('vote').querySelector('input').focus();
  };
  el('vote').onsubmit = event => {
    event.preventDefault();
    emit(event.submitter, () => confirmAnswer(Object.fromEntries(new FormData(el('vote')))));
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
  window.addEventListener('hashchange', () => { if (detail) display(detail); });
  load().catch(error => { el('status').textContent = error.message; });
})();
