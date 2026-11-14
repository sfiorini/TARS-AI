'use strict';
document.addEventListener('DOMContentLoaded', function () {
  var pool = {};
  var defaultsData = null;
  var defaultsOk = false;
  function el(id) { return document.getElementById(id); }
  function renderError() {
    var list = el('meLibList');
    if (!list) return;
    list.textContent = 'Failed to load movements.';
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
  function renderLibrary() {}
  fetchPool();
});