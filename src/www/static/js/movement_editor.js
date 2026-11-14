'use strict';
document.addEventListener('DOMContentLoaded', function () {
  var pool = {};
  var defaultsData = null;
  var defaultsOk = false;
  var defaultsFailureNotified = false;
  function el(id) { return document.getElementById(id); }
  var selectedType = 'all';
  var selectedSort = 'id';
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
    var target = event.target;
    var retry = target.closest('#meLibRetry');
    if (retry) { fetchPool(); return; }
    var create = target.closest('#meNewBtn');
    if (create) {
      var newId = el('mvNewId');
      if (newId) { newId.focus(); newId.scrollIntoView({behavior: 'smooth', block: 'center'}); }
      return;
    }
    var typeButton = target.closest('#meLibTypeFilter [data-type]');
    if (typeButton) {
      selectedType = typeButton.getAttribute('data-type');
      renderLibrary();
      return;
    }
    var actionButton = target.closest('[data-action]');
    if (!actionButton) return;
    var action = actionButton.getAttribute('data-action');
    if (action === 'retry') { fetchPool(); return; }
    if (action === 'clear-filters') {
      selectedType = 'all';
      if (el('meLibSearch')) el('meLibSearch').value = '';
      renderLibrary();
      return;
    }
    var row = actionButton.closest('.meLibRow');
    if (!row) return;
    var id = row.getAttribute('data-mid');
    if (action === 'play') playMovement(id, actionButton);
    else if (action === 'edit') openLegacyEditor(id);
    else if (action === 'dup') duplicateMovement(id, actionButton);
    else if (action === 'del') deleteMovement(id);
  }
  function matchesFilters(id, entry) {
    var search = el('meLibSearch');
    var query = search ? (search.value || '').trim().toLocaleLowerCase() : '';
    var searchable = [id, entry.name || '', entry.description || ''].join(' ').toLocaleLowerCase();
    var textMatches = !query || searchable.indexOf(query) !== -1;
    var typeMatches = selectedType === 'all' || entry.type === selectedType;
    return textMatches && typeMatches;
  }
  function sortKey(id, entry) {
    if (selectedSort === 'name') return (entry.name || '').toLocaleLowerCase();
    if (selectedSort === 'type') return (entry.type || '').toLocaleLowerCase();
    return id.toLocaleLowerCase();
  }
  function compareEntries(a, b) {
    return sortKey(a.id, a.entry).localeCompare(sortKey(b.id, b.entry)) || a.id.localeCompare(b.id);
  }
  function entrySummary(entry) {
    var steps = Array.isArray(entry.steps) ? entry.steps : [];
    var armsSteps = Array.isArray(entry.steps_arms) ? entry.steps_arms : [];
    var hold = 0;
    [steps, armsSteps].forEach(function (branch) {
      branch.forEach(function (step) {
        var seconds = Number(step && step.hold_time);
        if (isFinite(seconds)) hold += Math.min(10, Math.max(0, seconds));
      });
    });
    return {steps: steps.length, hold: hold};
  }
  function makeActionButton(action) {
    var actions = {
      play: {icon: '▶', label: 'Play'},
      edit: {icon: '✎', label: 'Edit'},
      dup: {icon: '⧉', label: 'Duplicate'},
      del: {icon: '🗑', label: 'Delete'}
    };
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'hud-btn hud-btn-secondary me-action-btn';
    button.setAttribute('data-action', action);
    button.setAttribute('aria-label', actions[action].label);
    button.title = actions[action].label;
    button.textContent = actions[action].icon;
    return button;
  }
  function renderLibrary() {
    var list = el('meLibList'); if (!list) return;
    list.innerHTML = '';
    var ids = Object.keys(pool);
    var filtered = ids.map(function (id) { return {id: id, entry: pool[id]}; })
      .filter(function (item) { return matchesFilters(item.id, item.entry); })
      .sort(compareEntries);
    var typeButtons = el('meLibTypeFilter');
    if (typeButtons) Array.prototype.forEach.call(typeButtons.querySelectorAll('[data-type]'), function (button) {
      button.setAttribute('aria-pressed', button.getAttribute('data-type') === selectedType ? 'true' : 'false');
    });
    var count = el('meLibCount');
    if (count) {
      var legs = 0; var arms = 0;
      ids.forEach(function (id) { if (pool[id].type === 'legs_only') legs++; else if (pool[id].type === 'has_arms') arms++; });
      var isFiltered = selectedType !== 'all' || (el('meLibSearch') && (el('meLibSearch').value || '').trim());
      count.textContent = isFiltered ? filtered.length + ' of ' + ids.length : ids.length + ' movements · ' + legs + ' legs · ' + arms + ' arms';
    }
    if (!filtered.length) {
      var empty = document.createElement('div'); empty.className = 'me-empty-state';
      var message = document.createElement('span'); message.textContent = 'No movements match.';
      empty.appendChild(message);
      if (selectedType !== 'all' || (el('meLibSearch') && el('meLibSearch').value)) {
        var clear = document.createElement('button'); clear.type = 'button';
        clear.className = 'hud-btn hud-btn-secondary'; clear.setAttribute('data-action', 'clear-filters');
        clear.textContent = 'Clear filters'; empty.appendChild(clear);
      }
      list.appendChild(empty);
      return;
    }
    filtered.forEach(function (item) {
      var id = item.id; var entry = item.entry;
      var row = document.createElement('div'); row.className = 'meLibRow'; row.setAttribute('data-mid', id);
      var heading = document.createElement('div'); heading.className = 'me-row-main';
      var idLabel = document.createElement('span'); idLabel.className = 'me-id'; idLabel.textContent = id;
      var type = document.createElement('span');
      type.className = 'me-type-chip' + (entry.type === 'has_arms' ? ' me-type-arms' : '');
      type.textContent = entry.type === 'has_arms' ? 'ARMS' : 'LEGS';
      var badge = document.createElement('span'); badge.className = 'me-status-badge';
      badge.setAttribute('data-default-status', 'unknown'); badge.style.display = defaultsOk ? '' : 'none';
      heading.appendChild(idLabel); heading.appendChild(type); heading.appendChild(badge);
      var detail = document.createElement('div'); detail.className = 'me-row-detail';
      var name = document.createElement('span'); name.className = 'me-name-description';
      name.textContent = (entry.name || id) + ' — ' + (entry.description || '');
      var summaryValue = entrySummary(entry);
      var summary = document.createElement('span'); summary.className = 'me-summary';
      summary.textContent = summaryValue.steps + ' steps · ' + summaryValue.hold.toFixed(1) + 's hold';
      detail.appendChild(name); detail.appendChild(summary);
      var actions = document.createElement('div'); actions.className = 'me-row-actions';
      ['play', 'edit', 'dup', 'del'].forEach(function (action) { actions.appendChild(makeActionButton(action)); });
      row.appendChild(heading); row.appendChild(detail); row.appendChild(actions); list.appendChild(row);
    });
    applyBadges();
  }
  function openLegacyEditor(id) {
    var entry = pool[id];
    if (!entry) return;
    var list = el('mvPoolList');
    if (list) {
      var rows = list.querySelectorAll('.bld-step-row');
      for (var index = 0; index < rows.length; index++) {
        var label = rows[index].querySelector('span b');
        var buttons = rows[index].querySelectorAll('button');
        if (label && label.textContent === id && buttons.length > 1) {
          buttons[1].click();
          return;
        }
      }
    }
    el('mvEditorId').textContent = '"' + id + '"';
    el('mvName').value = entry.name || '';
    el('mvType').value = entry.type || 'legs_only';
    el('mvDescription').value = entry.description || '';
    el('mvSteps').value = JSON.stringify(entry.steps || [], null, 2);
    el('mvStepsArms').value = entry.steps_arms == null ? '' : JSON.stringify(entry.steps_arms, null, 2);
    el('mvEditor').style.display = '';
    el('mvEditor').scrollIntoView({block: 'nearest'});
  }
  function notify(message, type) {
    if (window.showToast) window.showToast(message, type || 'info');
  }
  function stableStringify(value) {
    if (value === null || typeof value !== 'object') return JSON.stringify(value);
    if (Array.isArray(value)) return '[' + value.map(stableStringify).join(',') + ']';
    return '{' + Object.keys(value).sort().map(function (key) {
      return JSON.stringify(key) + ':' + stableStringify(value[key]);
    }).join(',') + '}';
  }
  function applyBadges() {
    var list = el('meLibList');
    if (!list) return;
    var rows = list.querySelectorAll('.meLibRow');
    Array.prototype.forEach.call(rows, function (row) {
      var badge = row.querySelector('[data-default-status]');
      if (!badge) return;
      if (!defaultsOk || !defaultsData) {
        badge.style.display = 'none';
        badge.textContent = '';
        badge.removeAttribute('title');
        return;
      }
      var id = row.getAttribute('data-mid');
      var inDefaults = Object.prototype.hasOwnProperty.call(defaultsData, id);
      var status = !inDefaults ? 'custom' :
        (stableStringify(pool[id]) === stableStringify(defaultsData[id]) ? 'default' : 'modified');
      badge.setAttribute('data-default-status', status);
      badge.textContent = status.toUpperCase();
      badge.style.display = '';
      badge.title = status === 'modified'
        ? 'Shipped movement, edited on this robot. Reset restores it.' : '';
    });
  }
  function fetchDefaults() {
    return fetch('/movements/defaults').then(function (r) {
      if (!r.ok) throw new Error('Failed to load movement defaults.');
      return r.json();
    }).then(function (d) {
      if (!d || !d.movements || typeof d.movements !== 'object') {
        throw new Error('Invalid movement defaults response.');
      }
      defaultsData = d.movements;
      defaultsOk = true;
      defaultsFailureNotified = false;
      applyBadges();
      return true;
    }).catch(function () {
      defaultsData = null;
      defaultsOk = false;
      applyBadges();
      if (!defaultsFailureNotified) {
        defaultsFailureNotified = true;
        notify('Movement defaults unavailable; status badges are hidden.', 'error');
      }
      return false;
    });
  }
  function playMovement(id, button) {
    if (button && button.disabled) return;
    if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
    return fetch('/play_saved_sequence', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: id})
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (d) { return {ok: false, d: d}; });
      return r.json().then(function (d) { return {ok: true, d: d}; });
    }).then(function (res) {
      if (!res.ok) notify(res.d.error || 'Playback failed.', 'error');
      else notify('Played "' + id + '"', 'success');
    }).catch(function (error) {
      notify(error && error.message ? error.message : 'Playback failed.', 'error');
    }).finally(function () {
      if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
    });
  }
  function deepCopy(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }
  function duplicateMovement(id, button) {
    var entry = pool[id];
    if (!entry) { notify('Movement is no longer in the library.', 'error'); return; }
    var newId = id + '_copy';
    var suffix = 2;
    while (Object.prototype.hasOwnProperty.call(pool, newId)) newId = id + '_copy' + suffix++;
    if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
    var payload = {
      id: newId,
      name: (entry.name || id) + ' (copy)',
      type: entry.type,
      description: entry.description,
      steps: deepCopy(entry.steps),
      steps_arms: deepCopy(entry.steps_arms)
    };
    return fetch('/movements', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (d) { return {ok: false, d: d}; });
      return r.json().then(function (d) { return {ok: true, d: d}; });
    }).then(function (res) {
      if (!res.ok) { notify(res.d.error || 'Duplicate failed.', 'error'); return false; }
      notify('Duplicated as ' + newId, 'success');
      return fetchPool();
    }).catch(function (error) {
      notify(error && error.message ? error.message : 'Duplicate failed.', 'error');
      return false;
    }).finally(function () {
      if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
    });
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
  if (el('movements')) el('movements').addEventListener('click', handleLibraryClick);
  if (el('meLibSearch')) el('meLibSearch').addEventListener('input', renderLibrary);
  if (el('meLibSort')) el('meLibSort').addEventListener('change', function () {
    selectedSort = el('meLibSort').value || 'id'; renderLibrary();
  });
  if (el('meResetBtn')) el('meResetBtn').onclick = function () { resetOrPrune(false); };
  if (el('mePruneBtn')) el('mePruneBtn').onclick = function () { resetOrPrune(true); };
  fetchPool(); fetchDefaults();




});