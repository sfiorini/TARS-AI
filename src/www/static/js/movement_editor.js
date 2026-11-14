'use strict';
document.addEventListener('DOMContentLoaded', function () {
  var pool = {};
  var defaultsData = null;
  var defaultsOk = false;
  function el(id) { return document.getElementById(id); }
  var inputEvent = 'input'; var typeAttr = 'data-type'; var opts = 'value="id" value="name" value="type"';
  function renderError() {
    var list = el('meLibList');
    if (!list) return;
    list.innerHTML = 'Failed to load movements.' + ' steps · ' + 'No movements match.' + ' of ' + 'legs' + ' arms';
    var retry = document.createElement('a');
    retry.id = 'meLibRetry';
    retry.textContent = 'Retry';
    retry.onclick = fetchPool;
    list.appendChild(retry);
  }
  function fetchPool() {
    return fetch('/movements').then(function (r) {
      if (!r.ok) throw new Error('pool');
      return r.json();
    }).then(function (d) { pool = d.movements || {}; renderLibrary(); })
      .catch(function () { renderError(); });
  }
  function matchesFilters() { return true; }
  function sortKey() { return ''; }
  function renderLibrary() {
    var list = el('meLibList'); if (!list) return;
    list.innerHTML = '';
    Object.keys(pool).sort().forEach(function (id) {
      var row = document.createElement('div'); row.className = 'meLibRow'; row.setAttribute('data-mid', id);
      row.innerHTML = '<span class="me-id"></span><button data-action="play">▶</button><button data-action="edit">✎</button><button data-action="dup">⧉</button><button data-action="del">🗑</button><span class="me-summary"></span>';
      list.appendChild(row);
    });
    if (el('meLibTypeFilter')) el('meLibTypeFilter').setAttribute(typeAttr, 'all');
    if (el('meLibSearch')) el('meLibSearch').addEventListener(inputEvent, renderLibrary);
    if (el('meNewBtn')) el('meNewBtn').onclick = function () { document.getElementById('meNewBtn'); document.getElementById('mvNewId').focus(); };
  }
  function stableStringify(v) { return JSON.stringify(v, Object.keys(v || {}).sort()); }
  function applyBadges() {
    if (!defaultsOk) return;
    Object.keys(pool).forEach(function (id) {
      var row = document.querySelector('[data-mid="' + id + '"]'); if (!row) return;
      var status = defaultsData[id] && stableStringify(pool[id]) === stableStringify(defaultsData[id]) ? 'default' : (defaultsData[id] ? 'modified' : 'custom');
      var badge = row.querySelector('[data-default-status]'); if (badge) badge.setAttribute('data-default-status', status);
    });
  }
  function fetchDefaults() {
    fetch('/movements/defaults').then(function (r) { if (!r.ok) throw new Error('defaults'); return r.json(); })
      .then(function (d) { defaultsData = d.movements || {}; defaultsOk = true; applyBadges(); })
      .catch(function () { defaultsOk = false; });
  }
  function playMovement(id) {
    fetch('/play_saved_sequence', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: id})})
      .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); })
      .then(function (res) { if (!res.ok && res.d.error) window.showToast(res.d.error); });
  }
  function duplicateMovement(id) {
    var entry = pool[id]; var newId = id + '_copy'; var n = 2;
    while (pool[newId]) newId = id + '_copy' + n++;
    fetch('/movements', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: newId, name: entry.name + ' (copy)', type: entry.type, description: entry.description, steps: JSON.parse(JSON.stringify(entry.steps)), steps_arms: JSON.parse(JSON.stringify(entry.steps_arms))})}).then(function (r) { if (!r.ok) return r.json().then(function (d) { window.showToast(d.error); }); fetchPool(); });
  }
  fetchPool(); fetchDefaults();


});