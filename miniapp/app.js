'use strict';

// Browser-only demo. No Bot API, user session, server writes, or client messages.
const initialChannels = [
  { id: 1, name: '@briefs_daily', caption: 'Проекты по разработке', enabled: true },
  { id: 2, name: '@remote_work_feed', caption: 'Удалённые заказы', enabled: true },
  { id: 3, name: '@product_tasks', caption: 'Продуктовые задачи', enabled: true },
  { id: 4, name: '@archive_leads', caption: 'Редкие публикации', enabled: false },
];
const sampleStats = {
  day: { matched: 6, applied: 2, label: 'за 24 часа', start: '24 часа назад', bars: [1, 2, 1, 2, 1, 3, 1, 2, 1, 2, 1, 1] },
  week: { matched: 27, applied: 11, label: 'за 7 дней', start: '7 дней назад', bars: [8, 12, 9, 17, 14, 19, 15] },
  month: { matched: 112, applied: 39, label: 'за 30 дней', start: '30 дней назад', bars: [32, 41, 36, 48, 39, 51, 45, 46, 43] },
};
const initialDraft = 'Здравствуйте! Могу разработать Telegram-бота для записи клиентов: расписание, напоминания и панель администратора. Подскажите, какие услуги нужно добавить и нужна ли интеграция с CRM? После уточнения сценариев предложу этапы, срок и стоимость.';
const $ = selector => document.querySelector(selector);
let channels = initialChannels.map(channel => ({ ...channel }));
let nextChannelId = 5;
let monitoring = true;
let dismissed = false;
let replyDraft = initialDraft;
let toastTimer;

function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove('show'), 3500);
}

function showScreen() {
  const requested = location.hash.slice(1);
  const screen = ['overview', 'orders', 'channels'].includes(requested) ? requested : 'overview';
  document.querySelectorAll('.app-screen').forEach(element => {
    element.hidden = element.id !== `screen-${screen}`;
  });
  document.querySelectorAll('[data-screen]').forEach(link => {
    if (link.dataset.screen === screen) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  const labels = { overview: 'Обзор', orders: 'Заказы', channels: 'Каналы' };
  document.title = `${labels[screen]} — Freelance AI Scout · Демо`;
  window.scrollTo(0, 0);
}

function updateCounts() {
  const active = channels.filter(channel => channel.enabled).length;
  const word = active % 10 === 1 && active % 100 !== 11 ? 'активен'
    : active % 10 >= 2 && active % 10 <= 4 && (active % 100 < 12 || active % 100 > 14) ? 'активны' : 'активных';
  $('#channel-count').textContent = `${String(active).padStart(2, '0')} / ${String(channels.length).padStart(2, '0')}`;
  $('#active-count').textContent = `${active} ${word}`;
  $('#status-channel-count').textContent = `${active} из ${channels.length}`;
}

function renderChannels(focusId, focusAction = 'toggle') {
  const query = $('#channel-search').value.trim().toLowerCase();
  const filtered = channels.filter(channel => `${channel.name} ${channel.caption}`.toLowerCase().includes(query));
  const list = $('#channel-list');
  list.replaceChildren();
  filtered.forEach(channel => {
    const row = document.createElement('div');
    row.className = 'channel-row';
    const icon = document.createElement('span');
    icon.className = 'channel-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = channel.name.replace('@', '').charAt(0).toUpperCase();
    const name = document.createElement('span');
    name.className = 'channel-name';
    const title = document.createElement('strong');
    title.textContent = channel.name;
    const caption = document.createElement('small');
    caption.textContent = `${channel.caption} · ${channel.enabled ? 'активен' : 'на паузе'}`;
    name.append(title, caption);
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'switch';
    toggle.dataset.channelId = channel.id;
    toggle.dataset.action = 'toggle';
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-label', `${channel.enabled ? 'Отключить' : 'Включить'} ${channel.name}`);
    toggle.setAttribute('aria-checked', String(channel.enabled));
    toggle.innerHTML = '<span aria-hidden="true"></span>';
    toggle.addEventListener('click', () => {
      channel.enabled = !channel.enabled;
      renderChannels(channel.id);
      toast(`${channel.name} ${channel.enabled ? 'включён' : 'на паузе'} в демо`);
    });
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'remove-channel';
    remove.dataset.channelId = channel.id;
    remove.dataset.action = 'remove';
    remove.setAttribute('aria-label', `Удалить ${channel.name} из демо`);
    remove.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-trash"/></svg>';
    remove.addEventListener('click', () => {
      const index = filtered.indexOf(channel);
      const next = filtered[index + 1] || filtered[index - 1];
      channels = channels.filter(item => item.id !== channel.id);
      renderChannels(next?.id, 'remove');
      if (!next) $('#show-add-form').focus();
      toast(`${channel.name} удалён из демо. «Сбросить демо» вернёт список.`);
    });
    row.append(icon, name, toggle, remove);
    list.append(row);
  });
  $('#empty-search').hidden = filtered.length > 0;
  updateCounts();
  if (focusId !== undefined) list.querySelector(`[data-channel-id="${focusId}"][data-action="${focusAction}"]`)?.focus();
}

function setMonitoring(value) {
  monitoring = value;
  $('.status-panel').classList.toggle('is-paused', !value);
  $('#status-title').textContent = value ? 'Активен в демо' : 'На паузе в демо';
  $('#status-subtitle').textContent = value ? 'Проверка каждые 10 минут' : 'Изменено только состояние макета';
  $('#toggle-monitor').setAttribute('aria-pressed', String(!value));
  $('#toggle-monitor span').textContent = value ? 'Поставить на паузу' : 'Возобновить';
  $('#toggle-monitor use').setAttribute('href', value ? '#i-pause' : '#i-play');
}

function setPeriod(period) {
  const data = sampleStats[period];
  const found = data.bars.reduce((sum, value) => sum + value, 0);
  $('#stat-found').textContent = found;
  $('#stat-matched').textContent = data.matched;
  $('#stat-applied').textContent = data.applied;
  $('#activity-label').textContent = data.label;
  $('#activity-start').textContent = data.start;
  document.querySelectorAll('[data-period]').forEach(button => {
    const active = button.dataset.period === period;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  const bars = $('#activity-bars');
  bars.replaceChildren();
  const maximum = Math.max(...data.bars);
  data.bars.forEach(value => {
    const bar = document.createElement('i');
    bar.style.setProperty('--h', `${value / maximum * 100}%`);
    bar.setAttribute('aria-hidden', 'true');
    bars.append(bar);
  });
  bars.setAttribute('aria-label', `Демонаходки ${data.label}: ${found} всего. Равные интервалы, от старых к новым: ${data.bars.join(', ')}.`);
}

function showDialog(title, description, withDraft = false) {
  $('#dialog-title').textContent = title;
  $('#dialog-description').textContent = description;
  $('#draft-fields').hidden = !withDraft;
  $('#dialog-confirm').hidden = withDraft;
  $('#reply-draft').value = replyDraft;
  $('#order-dialog').showModal();
  if (withDraft) $('#reply-draft').focus();
}

$('#today-date').textContent = new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric', month: 'long', timeZone: 'Asia/Yekaterinburg',
}).format(new Date());
window.addEventListener('hashchange', showScreen);
$('#match-reason').addEventListener('click', () => {
  const open = $('#match-reason').getAttribute('aria-expanded') === 'true';
  $('#match-reason').setAttribute('aria-expanded', String(!open));
  $('#reason-detail').hidden = open;
});
$('#respond-order').addEventListener('click', () => showDialog(
  'Сначала — хороший отклик.', 'Отредактируй пример под себя. Сохранение действует до перезагрузки страницы.', true,
));
$('#save-draft').addEventListener('click', () => {
  replyDraft = $('#reply-draft').value;
  $('#order-dialog').close();
  toast('Черновик сохранён в макете. Заказчику ничего не отправлено.');
});
$('#open-order').addEventListener('click', () => showDialog(
  'Здесь будет исходный заказ.', 'Этот заказ вымышлен для демонстрации дизайна. Реальная ссылка появится после подключения к боту.',
));
function updateDismissed() {
  $('#dismiss-order').textContent = dismissed ? 'Вернуть в подборку' : 'Не подходит';
  $('#dismiss-order').setAttribute('aria-pressed', String(dismissed));
  $('#order-state').textContent = dismissed ? 'ОТМЕЧЕНО: НЕ ПОДХОДИТ' : 'НОВАЯ НАХОДКА';
}
$('#dismiss-order').addEventListener('click', () => {
  dismissed = !dismissed;
  updateDismissed();
  toast(dismissed ? 'Отмечено «Не подходит» в демо' : 'Отметка снята в демо');
});
$('#toggle-monitor').addEventListener('click', () => {
  setMonitoring(!monitoring);
  toast(monitoring ? 'Мониторинг возобновлён в демо' : 'Мониторинг на паузе в демо');
});
document.querySelectorAll('[data-period]').forEach(button => button.addEventListener('click', () => setPeriod(button.dataset.period)));
$('#find-orders').addEventListener('click', () => { location.hash = 'orders'; });
$('#channel-search').addEventListener('input', () => renderChannels());
$('#show-add-form').addEventListener('click', () => {
  const form = $('#add-channel-form');
  form.hidden = !form.hidden;
  $('#show-add-form').setAttribute('aria-expanded', String(!form.hidden));
  $('#show-add-form span').textContent = form.hidden ? 'Добавить канал' : 'Отмена';
  if (!form.hidden) $('#new-channel').focus();
});
$('#new-channel').addEventListener('input', () => {
  $('#add-error').textContent = '';
  $('#new-channel').removeAttribute('aria-invalid');
});
$('#add-channel-form').addEventListener('submit', event => {
  event.preventDefault();
  const field = $('#new-channel');
  const value = field.value.trim().replace(/^https?:\/\/t\.me\//i, '@').replace(/^(?!@)/, '@');
  const error = $('#add-error');
  if (!/^@[a-zA-Z][a-zA-Z0-9_]{4,31}$/.test(value)) {
    error.textContent = 'Укажи @username из 5–32 символов, начиная с латинской буквы.';
    field.setAttribute('aria-invalid', 'true');
    field.focus();
    return;
  }
  if (channels.some(channel => channel.name.toLowerCase() === value.toLowerCase())) {
    error.textContent = 'Этот канал уже в списке.';
    field.setAttribute('aria-invalid', 'true');
    field.focus();
    return;
  }
  channels.push({ id: nextChannelId++, name: value, caption: 'Добавлен в демо', enabled: true });
  field.value = '';
  field.removeAttribute('aria-invalid');
  error.textContent = '';
  $('#add-channel-form').hidden = true;
  $('#show-add-form').setAttribute('aria-expanded', 'false');
  $('#show-add-form span').textContent = 'Добавить канал';
  $('#channel-search').value = '';
  renderChannels();
  $('#show-add-form').focus();
  toast(`${value} добавлен в демо. Telegram не проверялся.`);
});
$('#reset-demo').addEventListener('click', () => {
  channels = initialChannels.map(channel => ({ ...channel }));
  nextChannelId = 5;
  $('#channel-search').value = '';
  $('#new-channel').value = '';
  $('#new-channel').removeAttribute('aria-invalid');
  $('#add-error').textContent = '';
  $('#add-channel-form').hidden = true;
  $('#show-add-form').setAttribute('aria-expanded', 'false');
  $('#show-add-form span').textContent = 'Добавить канал';
  replyDraft = initialDraft;
  dismissed = false;
  updateDismissed();
  $('#match-reason').setAttribute('aria-expanded', 'false');
  $('#reason-detail').hidden = true;
  setMonitoring(true);
  setPeriod('day');
  renderChannels();
  toast('Демо возвращено к исходному состоянию');
});

showScreen();
setPeriod('day');
renderChannels();
