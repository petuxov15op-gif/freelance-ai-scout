'use strict';

(() => {
  if (!window.scoutLive) return;
  const $ = selector => document.querySelector(selector);
  const tg = window.Telegram?.WebApp;
  const auth = tg?.initData || '';
  const periods = { day: ['за 24 часа', '24 часа назад'], week: ['за 7 дней', '7 дней назад'], month: ['за 30 дней', '30 дней назад'] };
  const labels = { new: 'Новый', interesting: 'Интересно', irrelevant: 'Не подходит', applied: 'Откликнулся', replied: 'Заказчик ответил', negotiating: 'Обсуждаем', won: 'Получил заказ', rejected: 'Отказ', no_reply: 'Не ответили' };
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let state, period = 'day', activeDraftId = null, timer, loadVersion = 0;

  function notify(message) {
    $('#toast').textContent = message;
    $('#toast').classList.add('show');
    clearTimeout(timer);
    timer = setTimeout(() => $('#toast').classList.remove('show'), 4500);
  }

  function failure(message) {
    $('#order-dialog').close();
    document.body.dataset.mode = 'error';
    $('#connection-panel').hidden = false;
    $('#connection-title').textContent = 'Не удалось открыть Scout.';
    $('#connection-description').textContent = message;
    $('#retry-connection').hidden = false;
    tg?.ready();
  }

  async function api(path, method = 'GET', data) {
    const response = await fetch(path, {
      method, headers: { Authorization: `tma ${auth}`, ...(data === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(data === undefined ? {} : { body: JSON.stringify(data) }), cache: 'no-store',
      signal: AbortSignal.timeout(150000),
    });
    const result = await response.json().catch(() => ({ error: 'Сервис недоступен. Проверь подключение бота и HTTPS-адрес.' }));
    if (response.ok && result.error) throw new Error(result.error);
    if (!response.ok) {
      const message = result.error || 'Действие не выполнено. Попробуй позже.';
      if ([401, 403].includes(response.status)) failure(message);
      throw new Error(message);
    }
    return result;
  }

  async function busy(button, action) {
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    try { await action(); }
    catch (error) { notify(error.name === 'TimeoutError' ? 'Ответ задерживается. Обнови данные перед повтором.' : error instanceof TypeError ? 'Нет соединения. Попробуй позже.' : error.message || 'Нет соединения. Попробуй позже.'); }
    finally { button.disabled = false; button.removeAttribute('aria-busy'); }
  }

  function screen() {
    const wanted = location.hash.slice(1);
    const current = ['overview', 'orders', 'channels'].includes(wanted) ? wanted : 'overview';
    document.querySelectorAll('.app-screen').forEach(el => { el.hidden = el.id !== `screen-${current}`; });
    document.querySelectorAll('[data-screen]').forEach(link => {
      if (link.dataset.screen === current) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    document.title = `${{ overview: 'Обзор', orders: 'Заказы', channels: 'Каналы' }[current]} — Scout`;
    if (current === 'overview') tg?.BackButton?.hide(); else tg?.BackButton?.show();
    window.scrollTo(0, 0);
  }

  function renderOverview() {
    const stats = state.statistics, monitoring = state.monitoring;
    const active = state.channels.filter(c => c.status === 'approved' && !c.platform_paused).length;
    const monitored = state.channels.filter(c => ['approved', 'paused'].includes(c.status)).length;
    $('.status-panel').classList.toggle('is-paused', monitoring.paused);
    $('#status-title').textContent = monitoring.paused ? 'Проверка каналов на паузе' : monitoring.running ? 'Проверяю каналы' : monitoring.errors ? 'Есть ошибки проверки' : monitoring.last_finished ? 'Проверка каналов включена' : 'Ожидаю первую проверку';
    $('#status-subtitle').textContent = monitoring.paused && monitoring.running ? 'Текущая операция завершится' : monitoring.last_finished ? `Последняя: ${new Date(monitoring.last_finished).toLocaleString('ru-RU', { timeZone: 'Asia/Yekaterinburg', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })} · Екб` : 'Интервал — 10 минут';
    if (state.telegram_reader && !state.telegram_reader.ready) {
      $('#status-title').textContent = 'Чтение Telegram не подключено';
      $('#status-subtitle').textContent = state.telegram_reader.error || 'Сохранённые заказы доступны';
    }
    $('#status-channel-count').textContent = `${active} из ${monitored}`;
    $('#toggle-monitor span').textContent = monitoring.paused ? 'Возобновить проверку' : 'Поставить на паузу';
    $('#toggle-monitor').setAttribute('aria-pressed', String(monitoring.paused));
    $('#toggle-monitor use').setAttribute('href', monitoring.paused ? '#i-play' : '#i-pause');
    $('#stat-found').textContent = stats.found;
    $('#stat-matched').textContent = stats.matched;
    $('#stat-applied').textContent = stats.applied;
    $('#activity-label').textContent = periods[period][0];
    $('#activity-start').textContent = periods[period][1];
    document.querySelectorAll('[data-period]').forEach(button => {
      button.classList.toggle('active', button.dataset.period === period);
      button.setAttribute('aria-pressed', String(button.dataset.period === period));
    });
    const maximum = Math.max(1, ...stats.bars);
    $('#activity-bars').replaceChildren(...stats.bars.map(value => {
      const bar = document.createElement('i');
      bar.style.setProperty('--h', `${value / maximum * 100}%`);
      bar.setAttribute('aria-hidden', 'true');
      return bar;
    }));
    $('#activity-bars').setAttribute('aria-label', `Прочитано публикаций ${periods[period][0]}: ${stats.found}. Равные интервалы от старых к новым: ${stats.bars.join(', ')}.`);
  }

  function renderJobs() {
    const jobs = state.jobs;
    $('#screen-orders').innerHTML = `<div class="section-top"><span class="eyebrow">ТВОИ ЗАКАЗЫ</span><span class="section-date">Всего: ${jobs.total}</span></div><h1 id="orders-title">Есть за что<br><span>взяться.</span></h1><p class="screen-copy">Разбор, отклики и решения — в одном месте.</p>`
      + (jobs.items.length ? jobs.items.map(job => {
        const budget = [job.budget, job.currency].filter(Boolean).join(' ') || 'Не указан';
        const time = job.published_at ? new Date(job.published_at).toLocaleString('ru-RU', { timeZone: 'Asia/Yekaterinburg', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : 'Не указано';
        return `<article class="bot-bubble"><div class="message-kicker"><span class="signal-dot" aria-hidden="true"></span>ЗАКАЗ #${job.id}</div><p class="live-status-label">${escape(labels[job.status] || job.status)}</p><div class="order-source">${escape(job.source)}</div><h2>${escape(job.title)}</h2><details class="live-details"><summary>Текст объявления</summary><p class="live-description">${escape(job.description)}</p></details><div class="order-facts"><div><span>БЮДЖЕТ</span><strong>${escape(budget)}</strong></div><div><span>ОПУБЛИКОВАН · ЕКБ</span><strong>${escape(time)}</strong></div></div><details class="live-details"><summary>ИИ-рейтинг: ${escape(job.score)}/100 · разбор</summary><p>${escape(job.score_reason)}</p>${job.requirements.length ? `<strong>Требования</strong><ul>${job.requirements.map(t => `<li>${escape(t)}</li>`).join('')}</ul>` : ''}${job.risks.length ? `<strong>Риски</strong><ul>${job.risks.map(t => `<li>${escape(t)}</li>`).join('')}</ul>` : ''}${job.questions.length ? `<strong>Уточнить</strong><ul>${job.questions.map(t => `<li>${escape(t)}</li>`).join('')}</ul>` : ''}</details><p class="message-footer">Способ отклика: ${escape(job.contact_label)}</p><div class="order-actions"><button class="primary-button" type="button" data-draft="${job.id}">Подготовить отклик</button><button class="outline-button" type="button" data-link="${job.id}" ${job.link ? '' : 'disabled'}>Исходный заказ</button><button class="quiet-button" type="button" data-dismiss="${job.id}">${job.status === 'irrelevant' ? 'Вернуть' : 'Не подходит'}</button></div><label class="live-status">Статус общения<select data-status="${job.id}" aria-label="Статус заказа ${job.id}">${Object.entries(labels).map(([key, label]) => `<option value="${key}" ${job.status === key ? 'selected' : ''}>${label}</option>`).join('')}</select></label></article>`;
      }).join('') : '<p class="live-empty">Проверенных заказов пока нет. Добавь канал и дождись следующей проверки.</p>')
      + (jobs.next_offset !== null ? '<button class="add-button" type="button" id="more-jobs">Показать ещё</button>' : '');
  }

  function renderChannels() {
    const query = $('#channel-search').value.trim().toLowerCase();
    const monitored = state.channels.filter(c => ['approved', 'paused'].includes(c.status));
    const active = monitored.filter(c => c.status === 'approved' && !c.platform_paused).length;
    $('#channel-count').textContent = `${active} / ${monitored.length}`;
    $('#active-count').textContent = `В работе: ${active}`;
    const filtered = monitored.filter(c => `${c.name} ${c.title}`.toLowerCase().includes(query));
    $('#channel-list').innerHTML = filtered.map(c => `<div class="channel-row"><span class="channel-name"><strong>${escape(c.name)}</strong><small>${escape(c.title)} · ${c.platform_paused ? 'общая пауза площадок' : c.status === 'approved' ? 'активен' : 'на паузе'}</small></span><button class="switch" type="button" role="switch" data-channel="${c.id}" aria-checked="${c.status === 'approved'}" aria-label="${c.status === 'approved' ? 'Отключить' : 'Включить'} ${escape(c.name)}"><span aria-hidden="true"></span></button></div>`).join('');
    $('#empty-search').hidden = filtered.length > 0;
    const pending = state.channels.filter(c => c.status === 'pending' && `${c.name} ${c.title}`.toLowerCase().includes(query));
    $('#live-candidates').innerHTML = pending.length ? `<h2 class="list-heading">На подтверждении · ${pending.length}</h2>` + pending.map(c => `<article class="live-candidate"><h3>${escape(c.title)}</h3><p>${escape(c.name)}</p><p>${escape(c.reason)}</p><details class="live-details"><summary>Посмотреть примеры</summary><pre>${escape(c.examples.join('\n\n') || 'Примеры недоступны')}</pre></details><div class="order-actions"><button type="button" class="primary-button" data-approve="${c.id}">Добавить</button><button type="button" class="outline-button" data-reject="${c.id}">Отклонить</button></div></article>`).join('') : '';
  }

  async function load() {
    if (!auth) throw new Error('Открой Mini App кнопкой «Открыть Scout» в меню бота. Данные входа Telegram отсутствуют.');
    const version = ++loadVersion;
    const result = await api(`/api/state?period=${period}`);
    if (version !== loadVersion) return;
    period = result.period;
    state = result;
    renderOverview(); renderJobs(); renderChannels();
    $('#live-updated').textContent = `Обновлено ${new Date(state.updated_at).toLocaleTimeString('ru-RU', { timeZone: 'Asia/Yekaterinburg', hour: '2-digit', minute: '2-digit' })} · Екб`;
    document.body.dataset.mode = 'live';
    $('#connection-panel').hidden = true;
    tg?.ready();
  }

  async function changeStatus(id, status) {
    await api(`/api/jobs/${id}/feedback`, 'POST', { status });
    await load();
    notify('Решение сохранено в боте');
  }

  // Replace demo-specific labels before exposing any live state.
  $('.demo-chip').textContent = 'Личный кабинет';
  $('.demo-label').textContent = 'SCOUT';
  $('#screen-overview .demo-note').textContent = 'Пауза останавливает плановые проверки каналов после текущей операции. Поиск новых источников работает отдельно.';
  $('.stat-card small').textContent = 'публикаций';
  $('.activity-head h2').textContent = 'Прочитано публикаций';
  $('#screen-channels .eyebrow').textContent = 'ТВОИ ИСТОЧНИКИ';
  $('#screen-channels .demo-note').textContent = 'Проверка нового канала может занять время и использовать ИИ. В мониторинг он попадёт после твоего подтверждения.';
  $('#show-add-form span').textContent = 'Проверить новый канал';
  const candidates = document.createElement('div');
  candidates.id = 'live-candidates';
  $('#channel-list').after(candidates);
  $('.app-footer > span').id = 'live-updated';
  $('#reset-demo').textContent = 'Обновить';
  $('#draft-fields .draft-note').textContent = 'Черновик сохраняется в базе бота. Заказчику ничего не отправляется.';
  $('#save-draft').textContent = 'Сохранить черновик';
  $('#order-dialog .eyebrow').textContent = 'ТВОЙ ОТКЛИК';
  $('#today-date').textContent = new Date().toLocaleDateString('ru-RU', { timeZone: 'Asia/Yekaterinburg', day: 'numeric', month: 'long' });
  tg?.setHeaderColor?.('#111115'); tg?.setBackgroundColor?.('#111115'); tg?.expand();
  tg?.BackButton?.onClick(() => { location.hash = 'overview'; });
  window.addEventListener('hashchange', screen);
  screen();

  $('#reset-demo').addEventListener('click', e => busy(e.currentTarget, load));
  $('#retry-connection').addEventListener('click', e => busy(e.currentTarget, load));
  $('#find-orders').addEventListener('click', () => { location.hash = 'orders'; });
  $('#toggle-monitor').addEventListener('click', e => busy(e.currentTarget, async () => {
    await api('/api/monitoring', 'POST', { paused: !state.monitoring.paused }); await load();
    notify(state.monitoring.paused ? 'Проверка каналов на паузе' : 'Проверка каналов возобновлена');
  }));
  document.querySelectorAll('[data-period]').forEach(button => button.addEventListener('click', () => busy(button, async () => {
    const previous = period; period = button.dataset.period;
    try { await load(); } catch (error) { period = state?.period || previous; throw error; }
  })));
  $('#screen-orders').addEventListener('click', e => {
    const button = e.target.closest('button'); if (!button) return;
    if (button.dataset.link) {
      const job = state.jobs.items.find(j => j.id === Number(button.dataset.link));
      if (job?.link) { if (tg?.openLink) tg.openLink(job.link); else window.open(job.link, '_blank', 'noopener,noreferrer'); }
    } else if (button.dataset.dismiss) {
      const job = state.jobs.items.find(j => j.id === Number(button.dataset.dismiss));
      busy(button, () => changeStatus(job.id, job.status === 'irrelevant' ? 'new' : 'irrelevant'));
    } else if (button.dataset.draft) busy(button, async () => {
      notify('Готовлю черновик. Это может занять время.');
      const id = Number(button.dataset.draft);
      const result = await api(`/api/jobs/${id}/draft`, 'POST');
      activeDraftId = id;
      $('#dialog-title').textContent = `Отклик на заказ #${id}`;
      $('#dialog-description').textContent = 'Проверь и отредактируй текст перед отправкой.';
      $('#draft-fields').hidden = false; $('#dialog-confirm').hidden = true;
      $('#reply-draft').value = result.draft;
      $('#order-dialog').showModal(); $('#reply-draft').focus();
    });
    else if (button.id === 'more-jobs') busy(button, async () => {
      const page = await api(`/api/jobs?offset=${state.jobs.next_offset}`);
      const existing = new Set(state.jobs.items.map(j => j.id));
      state.jobs = { ...page, items: [...state.jobs.items, ...page.items.filter(j => !existing.has(j.id))] };
      renderJobs();
    });
  });
  $('#screen-orders').addEventListener('change', async e => {
    const field = e.target.closest('[data-status]'); if (!field) return;
    await busy(field, () => changeStatus(Number(field.dataset.status), field.value));
    if (field.isConnected) field.value = state.jobs.items.find(j => j.id === Number(field.dataset.status))?.status || 'new';
  });
  $('#save-draft').addEventListener('click', e => busy(e.currentTarget, async () => {
    await api(`/api/jobs/${activeDraftId}/draft`, 'PUT', { draft: $('#reply-draft').value });
    $('#order-dialog').close(); notify('Черновик сохранён. Заказчику ничего не отправлено.');
  }));
  $('#channel-search').addEventListener('input', renderChannels);
  $('#screen-channels').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    let id, status;
    if (b.dataset.channel) { id = Number(b.dataset.channel); status = state.channels.find(c => c.id === id).status === 'approved' ? 'paused' : 'approved'; }
    else if (b.dataset.approve) { id = Number(b.dataset.approve); status = 'approved'; }
    else if (b.dataset.reject) { id = Number(b.dataset.reject); status = 'rejected'; }
    else return;
    busy(b, async () => { await api(`/api/channels/${id}/status`, 'POST', { status }); await load(); notify('Статус канала сохранён'); });
  });
  $('#show-add-form').addEventListener('click', () => {
    const form = $('#add-channel-form'); form.hidden = !form.hidden;
    $('#show-add-form').setAttribute('aria-expanded', String(!form.hidden));
    $('#show-add-form span').textContent = form.hidden ? 'Проверить новый канал' : 'Отмена';
    if (!form.hidden) $('#new-channel').focus();
  });
  $('#add-channel-form').addEventListener('submit', e => {
    e.preventDefault();
    busy($('#add-channel-form button'), async () => {
      $('#add-error').textContent = '';
      try {
        await api('/api/channels', 'POST', { username: $('#new-channel').value.trim() });
        $('#add-channel-form').hidden = true; $('#new-channel').value = ''; $('#channel-search').value = '';
        $('#show-add-form').setAttribute('aria-expanded', 'false'); $('#show-add-form span').textContent = 'Проверить новый канал';
        await load(); notify('Канал проверен. Подтверди добавление в карточке кандидата.');
      } catch (error) { $('#add-error').textContent = error.message; throw error; }
    });
  });
  load().catch(error => failure(error.message || 'Нет соединения с ботом. Попробуй позже.'));
})();
