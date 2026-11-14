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
  fetchPool();
});