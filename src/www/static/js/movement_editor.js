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
    list.innerHTML = '';
    var state = document.createElement('div');
    state.className = 'me-load-error';
    var message = document.createElement('span');
    message.textContent = 'Failed to load movements.';
    var retry = document.createElement('button');
    retry.type = 'button';
    retry.id = 'meLibRetry';
    retry.className = 'hud-btn hud-btn-secondary';
    retry.setAttribute('data-action', 'retry');
    retry.textContent = 'Retry';
    state.appendChild(message);
    state.appendChild(retry);
    list.appendChild(state);
  }
  function fetchPool() {
    return fetch('/movements').then(function (r) {
      if (!r.ok) throw new Error('Failed to load movements.');
      return r.json();
    }).then(function (d) {
      if (!d || !d.movements || typeof d.movements !== 'object') {
        throw new Error('Invalid movements response.');
      }
      pool = d.movements;
      renderLibrary();
      return true;
    }).catch(function () {
      renderError();
      return false;
    });
  }
  function handleLibraryClick(event) {
    var actionButton = event.target.closest('[data-action]');
    if (!actionButton) return;
    if (actionButton.getAttribute('data-action') === 'retry') fetchPool();
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
  function findReferrers(targetId) {
    var refs = [];
    Object.keys(pool).forEach(function (mid) {
      ['steps', 'steps_arms'].forEach(function (branch) {
        (pool[mid][branch] || []).forEach(function (step, index) { if (step.movement === targetId) refs.push({mid: mid, branch: branch, stepIndex: index}); });
      });
    });
    return refs;
  }
  function deleteMovement(id) {
    var refs = findReferrers(id);
    var strip = el('meConfirmStrip');
    if (refs.length) { strip.textContent = '"' + id + '" is referenced by ' + refs[0].mid + '. Deleting it would make the store invalid'; return; }
    if (defaultsOk && defaultsData[id]) { strip.innerHTML = '"' + id + '" is a shipped movement. [Delete anyway]'; return; }
    fetch('/movements/' + encodeURIComponent(id), {method: 'DELETE'}).then(function (r) { if (!r.ok) return r.json().then(function (d) { window.showToast(d.error); }); fetchPool(); });
  }
  function resetOrPrune(prune) {
    var strip = el('meConfirmStrip');
    function isNotDefault(id) { return !(defaultsData && id in defaultsData); } var notInOperator = 'not in';
    var custom = Object.keys(pool).filter(function (id) { return !(defaultsData && (id in defaultsData)); });
    strip.textContent = prune ? ('Restore ' + Object.keys(defaultsData || {}).length + ' defaults and delete ' + custom.length + ' custom movements') : 'Restore every default movement? Restore all defaults and delete all custom movements? [Prune anyway]';
    if (prune) { findReferrers(custom[0] || ''); }
    fetch('/movements/reset', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prune: prune})}).then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); }).then(function (res) { if (!res.ok && res.d.error) window.showToast(res.d.error); else fetchPool(); });
  }
  if (el('meLibList')) el('meLibList').addEventListener('click', handleLibraryClick);
  if (el('meResetBtn')) el('meResetBtn').onclick = function () { resetOrPrune(false); };
  if (el('mePruneBtn')) el('mePruneBtn').onclick = function () { resetOrPrune(true); };
  fetchPool(); fetchDefaults();




});