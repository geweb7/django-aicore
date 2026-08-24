// ── Фоновые задачи (polling) — общий слой aicore, требует jQuery ─────────────
// Как этим пользоваться из приложения — aicore/README.md.
// Свой xhrErr/netErrMsg в приложении не писать: берите отсюда.

function netErrMsg(xhr, context) {
  var srv = xhr.responseJSON && xhr.responseJSON.error;
  if (srv) return srv;
  if (xhr.status === 0) return 'Соединение с сервером прервано (' + context + '). Задача на сервере могла продолжить выполняться — обновите страницу.';
  var msg = 'HTTP ' + xhr.status + ' (' + context + ')';
  if (xhr.status === 403) msg += ' — запрос отклонён сервером';
  if (xhr.status === 502) msg += ' — Bad Gateway';
  if (xhr.status === 504) msg += ' — Gateway Timeout';
  return msg;
}

// Всё сырое, что известно про упавшую задачу: ответ модели и traceback с сервера.
// Разные вещи, поэтому с заголовками, а не одним безымянным блоком.
function taskErrorRaw(pr) {
  var parts = [];
  if (pr.raw_response) parts.push('Сырой ответ модели:\n' + pr.raw_response);
  if (pr.traceback) parts.push('Traceback (сервер):\n' + pr.traceback);
  return parts.join('\n\n────────────────────\n\n');
}

// Одна строка фактов о вызове: модель, температура, токены, хостер, ретраи, время.
// Единственный формат на все приложения — своего usageStr не заводить. Отдаёт ТЕКСТ,
// не HTML: вставлять через .text() либо экранировать самому.
function usageLine(u) {
  if (!u) return '';
  var parts = [];
  if (u.model) parts.push(u.model);
  if (u.temperature != null) parts.push('t=' + u.temperature);
  var tok = [];
  if (u.prompt_tokens != null) tok.push('промпт: ' + u.prompt_tokens);
  if (u.completion_tokens != null) tok.push('completion: ' + u.completion_tokens);
  if (u.reasoning_tokens != null) tok.push('из них reasoning: ' + u.reasoning_tokens);
  if (tok.length) parts.push(tok.join(' / ') + ' tok');
  // Цену называет провайдер (сейчас только OpenRouter). Нет ключа — цены не было;
  // подставлять сюда ноль нельзя, бесплатный вызов и неназванная цена — разное.
  if (u.cost != null) parts.push('$' + Number(u.cost).toFixed(5));
  if (u.upstream) parts.push('хостер: ' + u.upstream);
  if (u.retries) parts.push('прокси перебрано: ' + u.retries);
  if (u.elapsed_s != null) parts.push(u.elapsed_s + ' c');
  return parts.join(' · ');
}

function pollTask(url, onDone, onFail) {
  $.get(url, function(pr) {
    if (pr.status === 'running') { setTimeout(function() { pollTask(url, onDone, onFail); }, 2000); return; }
    if (pr.status === 'done') onDone(pr.result);
    // Упавшая задача несёт usage вызова — четвёртым аргументом, чтобы блок ошибки
    // показывал токены тем же способом, что и блок успеха. resume тут null: задача
    // честно упала на сервере, возобновлять нечего.
    else onFail(pr.error || 'Ошибка', taskErrorRaw(pr), null, pr.usage);
  }).fail(function(xhr) {
    // resume отдаём только там, где задача на сервере может быть жива и её результат
    // лежит на диске: обрыв связи и сбой шлюза. На 403 (истёк CSRF) и 404 (файла задачи
    // больше нет) повторный GET даст то же самое — кнопка возобновления там врала бы.
    // Зовёт resume только пользователь: автоматического ретрая тут нет и не должно быть.
    var alive = xhr.status === 0 || xhr.status === 502 || xhr.status === 504;
    var resume = alive ? function() { pollTask(url, onDone, onFail); } : null;
    onFail(netErrMsg(xhr, 'опрос задачи'), xhr.responseText || '', resume);
  });
}

function startAndPoll(startUrl, postData, onDone, onFail) {
  $.post(startUrl, postData, function(resp) {
    if (resp.error) { onFail(resp.error); return; }
    pollTask(resp.poll_url, onDone, onFail);
  }).fail(function(xhr) {
    onFail(netErrMsg(xhr, 'запуск задачи'));
  });
}

function _esc(text) {
  return $('<span>').text(text == null ? '' : text).html();
}

function _collapseId(prefix) {
  return prefix + '-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6);
}

// Разметку слоя приложение вправе переопределять, поэтому у каждого узла две метки с
// разными ролями: класс `aicore-*` — зацепка для CSS, `data-aicore` — для JS. Разведены
// намеренно: класс оформления однажды переименуют, и он не должен утащить за собой
// обработчики приложения. Инлайн остаётся только там, где без него блок ломает страницу
// (высота коллапса) — всё остальное утилитами, иначе переопределить нельзя ничем, кроме
// !important. Своего CSS слой не везёт: aicore не про оформление.
function _collapseBtn(id, label, anchor) {
  return ' <button type="button" class="aicore-block__btn btn btn-link btn-sm p-0 align-baseline small"' +
    ' data-aicore="' + anchor + '" data-bs-toggle="collapse" data-bs-target="#' + id + '">' +
    _esc(label) + '</button>';
}

// Текст кладётся .text()-ом: в промпте и в ответе модели бывает и разметка, и
// незакрытый </pre> — всё это должно читаться буквами, а не исполняться.
function _collapsePre(id, text, anchor) {
  return '<div class="collapse" id="' + id + '"><pre class="aicore-block__pre border rounded p-1 mb-0 mt-1' +
    ' overflow-auto bg-white text-body" data-aicore="' + anchor + '"' +
    ' style="max-height:200px;white-space:pre-wrap">' + _esc(text) + '</pre></div>';
}

// Середина обоих блоков — одна: подпись, кнопки коллапсов, строка фактов вызова и сами
// коллапсы. Общая не «по договорённости о вызове одних хелперов», а физически: порядок,
// подписи и условия показа лежат здесь в единственном экземпляре, поэтому блок успеха и
// блок ошибки не могут разойтись. Отличаются они только обёрткой.
function _blockBody(head, raw, usage, kind) {
  var sent = (usage || {}).sent;
  var sentId = _collapseId(kind + '-sent');
  var rawId = _collapseId(kind + '-raw');
  var html = head;
  // Сначала что послали, потом что вернулось — в порядке чтения.
  if (sent) html += _collapseBtn(sentId, 'что отправлено (' + sent.length + ' знаков)', 'sent-btn');
  if (raw) html += _collapseBtn(rawId, 'сырой ответ', 'raw-btn');
  var facts = usageLine(usage);
  if (facts) {
    html += '<div class="aicore-block__facts text-muted small mt-1" data-aicore="facts">' +
      _esc(facts) + '</div>';
  }
  if (sent) html += _collapsePre(sentId, sent, 'sent-pre');
  if (raw) html += _collapsePre(rawId, raw, 'raw-pre');
  return html;
}

// Блок ошибки AI-вызова: текст ошибки, факты вызова и два коллапса — «что отправлено»
// (промпт из usage.sent) и «сырой ответ».
//
// Оба отладочных текста рисует слой. Раньше «что отправлено» дорисовывало приложение,
// и на упавшем вызове — том единственном месте, где промпт и нужен, — его чаще всего
// не оказывалось: забыть можно только то, что надо помнить отдельно. Мегабайт промпта
// в разметке не страшен: он и так уезжает на фронт внутри usage, а развёрнутым его
// видит только тот, кто нажал.
//
// usage необязателен: не передали — блок прежний, без фактов вызова и без промпта.
function errorBlockHtml(msg, raw, usage) {
  return '<div class="aicore-err alert alert-danger small p-2 mb-2" data-aicore="err-block">' +
    _blockBody(_esc(msg || 'Ошибка'), raw, usage, 'err') + '</div>';
}

// Тот же блок для удавшегося вызова — прозрачность требуется всегда, а не только на
// падении. Своих сборщиков «что отправлено / сырой ответ» в приложениях не заводить:
// их уже пять штук на три приложения, и наборы фактов в них не совпадают.
//
// Рамка нейтральная, а не зелёная: блок не выносит вердикт результату, а показывает
// факты вызова. Он уместен и над ответом «ничего не найдено» — вызов при этом удался.
function successBlockHtml(label, raw, usage) {
  return '<div class="aicore-success small text-muted border rounded p-2 mb-2" data-aicore="success-block">' +
    _blockBody(label ? '<strong>' + _esc(label) + '</strong>' : '', raw, usage, 'success') + '</div>';
}

// Блок ошибки сетевого вызова: тело ответа сервера показывается всегда.
function xhrErrorBlock(xhr, context) {
  return errorBlockHtml(netErrMsg(xhr, context), xhr.responseText || '(пустое тело ответа)');
}

// Блок ошибки + кнопка ручного возобновления опроса (resume из onFail у pollTask).
// Отдаёт jQuery-объект, а не строку: колбэк resume не пришить к HTML-строке.
// Без resume — просто блок ошибки, кнопки нет.
//
// onResume — необязательный колбэк, зовётся на клике перед возобновлением. Нужен,
// чтобы приложение снова заблокировало свои кнопки запуска: к моменту ошибки они уже
// разблокированы, и клик по ним поверх возобновлённого опроса дал бы второй прогон с
// новым AI-вызовом. Своими виджетами приложение распоряжается само — aicore в них не
// лезет: кто их гасит, тот и возвращает в рабочее состояние.
// usage — пятым и необязательным: у обрыва опроса его нет (сервер не ответил), у
// упавшей задачи есть, и оба случая приходят в один и тот же onFail.
function resumeErrorBlock(msg, raw, resume, onResume, usage) {
  var $box = $('<div>').append(errorBlockHtml(msg, raw, usage));
  if (!resume) return $box;
  var $btn = $('<button type="button" class="btn btn-outline-primary btn-sm">').text('Забрать результат');
  $btn.on('click', function() {
    $btn.prop('disabled', true).text('Забираю…');
    if (onResume) onResume();
    resume();
  });
  return $box.append($btn);
}
