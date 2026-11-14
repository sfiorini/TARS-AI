'use strict';
document.addEventListener('DOMContentLoaded', function () {
  var pool = {};
  var defaultsData = null;
  var defaultsOk = false;
  var defaultsFailureNotified = false;
  var pendingAction = null;
  var pendingClearSnapshot = null;
  var draft = null;
  var savedSnapshot = null;
  var selected = {branch: 'steps', index: 0};
  var editorMode = 'visual';
  var dragFrom = null;
  var pointerDragFrom = null;
  var pointerDragTo = null;
  var isCreating = false;
  var idManuallyEdited = false;
  var ID_PATTERN = /^[a-z0-9_]+$/;
  var SERVO_FIELDS = [
    {field: 'left_height', code: 'LH', label: 'Left height', group: 'legs', min: 1, max: 100, step: 1},
    {field: 'right_height', code: 'RH', label: 'Right height', group: 'legs', min: 1, max: 100, step: 1},
    {field: 'left_leg', code: 'LL', label: 'Left leg', group: 'legs', min: 1, max: 100, step: 1},
    {field: 'right_leg', code: 'RL', label: 'Right leg', group: 'legs', min: 1, max: 100, step: 1},
    {field: 'left_main', code: 'LM', label: 'Left main', group: 'arms', min: 1, max: 100, step: 1},
    {field: 'left_forearm', code: 'LF', label: 'Left forearm', group: 'arms', min: 1, max: 100, step: 1},
    {field: 'left_hand', code: 'LH2', label: 'Left hand', group: 'arms', min: 1, max: 100, step: 1},
    {field: 'right_main', code: 'RM', label: 'Right main', group: 'arms', min: 1, max: 100, step: 1},
    {field: 'right_forearm', code: 'RF', label: 'Right forearm', group: 'arms', min: 1, max: 100, step: 1},
    {field: 'right_hand', code: 'RH2', label: 'Right hand', group: 'arms', min: 1, max: 100, step: 1}
  ];
  var lastSetValues = {};
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
    if (target.closest('#meNewBtn')) { openCreate(); return; }
    if (target.closest('#meBackBtn')) { closeEditor(); return; }
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
    else if (action === 'edit') openForEdit(id);
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
      var row = document.createElement('div');
      row.className = 'meLibRow' + (draft && draft.id === id && isDirty() ? ' me-row-dirty' : '');
      row.setAttribute('data-mid', id);
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
  function DEFAULT_STEP() {
    return {
      left_height: 50, right_height: 50, left_leg: 50, right_leg: 50,
      left_main: null, left_forearm: null, left_hand: null,
      right_main: null, right_forearm: null, right_hand: null,
      speed: 0.85, hold_time: 0, movement: null, arm_speed: null
    };
  }
  function isDirty() {
    return !!draft && (stableStringify(draft.entry) !== savedSnapshot || (isCreating && !!draft.id));
  }
  function markDirty() {
    var dirty = isDirty();
    var dot = el('meDirtyDot');
    var save = el('meSaveBtn');
    if (dot) dot.hidden = !dirty;
    if (save) save.setAttribute('data-state', dirty ? 'DIRTY' : 'CLEAN');
    if (save && !isCreating) save.disabled = false;
    renderLibrary();
  }
  function slugify(text) {
    return String(text || '').trim().toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '');
  }
  function idTaken(id) {
    return Object.prototype.hasOwnProperty.call(pool, id);
  }
  function updateIdStatus() {
    var input = el('meIdInput');
    var error = el('meIdError');
    if (!input || !isCreating) return;
    draft.id = input.value;
    var message = idTaken(draft.id) ? 'id exists' :
      (!ID_PATTERN.test(draft.id) ? 'Use lowercase letters, digits, and underscores.' : '');
    if (error) error.textContent = message;
    el('meSaveBtn').disabled = !draft.id || !ID_PATTERN.test(draft.id) || idTaken(draft.id);
  }
  function renderEditor() {
    var pane = el('meEditor');
    if (!pane || !draft) return;
    pane.hidden = false;
    el('meEditorTitle').textContent = draft.id ? (draft.entry.name || draft.id) : 'New movement';
    var idInput = el('meIdInput');
    var idLock = el('meIdLock');
    idInput.hidden = !isCreating;
    idLock.hidden = isCreating;
    if (isCreating) {
      idInput.value = draft.id || '';
      idLock.textContent = '';
    } else {
      idLock.textContent = '🔒 ' + draft.id;
      idLock.title = 'Ids are referenced by the D-pad, gamepad and voice assistant; renaming breaks them. Use Duplicate to fork.';
    }
    el('meNameInput').value = draft.entry.name || '';
    el('meTypeSelect').value = draft.entry.type || 'legs_only';
    el('meDescInput').value = draft.entry.description || '';
    el('meDirtyDot').hidden = true;
    el('meSaveBtn').setAttribute('data-state', 'CLEAN');
    el('meSaveBtn').disabled = false;
    updateIdStatus();
    renderTimeline();
  }
  function handleMetadataInput(event) {
    if (!draft) return;
    var target = event.target;
    if (target.id === 'meIdInput' && isCreating) {
      idManuallyEdited = true;
      draft.id = target.value;
      updateIdStatus();
      markDirty();
      return;
    }
    if (target.id === 'meNameInput') {
      draft.entry.name = target.value;
      if (isCreating && !idManuallyEdited) {
        draft.id = slugify(target.value);
        el('meIdInput').value = draft.id;
        updateIdStatus();
      }
    } else if (target.id === 'meDescInput') draft.entry.description = target.value;
    else if (target.id === 'meTypeSelect') draft.entry.type = target.value;
    else return;
    target.classList.toggle('me-field-invalid', !String(target.value).trim());
    markDirty();
  }
  function selectedStep() {
    if (!draft || !Array.isArray(draft.entry.steps)) return null;
    return draft.entry.steps[selected.index] || null;
  }
  function stepSummary(step) {
    if (step && step.movement) return '⚡ ' + step.movement;
    function valueFor(field) {
      var value = step[field];
      if (value === null || value === undefined || value === 0) return '–';
      if (value === -1) return 'H';
      return value;
    }
    var parts = [
      'LH ' + valueFor('left_height'), 'RH ' + valueFor('right_height'),
      'LL ' + valueFor('left_leg'), 'RL ' + valueFor('right_leg')
    ];
    [['LM', 'left_main'], ['LF', 'left_forearm'], ['LH2', 'left_hand'],
      ['RM', 'right_main'], ['RF', 'right_forearm'], ['RH2', 'right_hand']].forEach(function (item) {
      if (step[item[1]] !== null && step[item[1]] !== undefined && step[item[1]] !== 0) {
        parts.push(item[0] + ' ' + valueFor(item[1]));
      }
    });
    parts.push('⏱ ' + valueFor('speed'), '⏸ ' + valueFor('hold_time'));
    return parts.join(' · ');
  }
  function deriveMode(field, value) {
    if (value === null || value === undefined || value === 0) return 'skip';
    if (value === -1) return 'hold';
    return 'set';
  }
  function setStepField(field, value) {
    var step = selectedStep();
    if (!step) return false;
    if (value === null) step[field] = null;
    else {
      step[field] = value;
      if (typeof value === 'number' && value > 0 && value !== -1) lastSetValues[field] = value;
    }
    return true;
  }
  function renderSelectedStep() {
    var panel = el('mePosePanel');
    var step = selectedStep();
    if (!panel) return;
    panel.innerHTML = '';
    if (!step) { panel.textContent = 'No position selected.'; return; }
    var title = document.createElement('h3');
    title.textContent = step.movement ? '⚡ ' + step.movement : 'Step ' + (selected.index + 1);
    var summary = document.createElement('p');
    summary.className = 'me-selected-summary';
    summary.textContent = stepSummary(step);
    panel.appendChild(title);
    panel.appendChild(summary);
    if (!step.movement) {
      var groupTitle = document.createElement('h4'); groupTitle.textContent = 'LEGS'; panel.appendChild(groupTitle);
      var legend = document.createElement('p'); legend.className = 'me-field-legend';
      legend.textContent = '0/SKIP = servo not driven'; panel.appendChild(legend);
      SERVO_FIELDS.filter(function (config) { return config.group === 'legs'; }).forEach(function (config) {
        var row = document.createElement('div'); row.className = 'meServoRow servo-card';
        row.setAttribute('data-field', config.field);
        var label = document.createElement('span'); label.className = 'me-servo-label';
        label.title = config.label; label.textContent = config.code + ' ' + config.label;
        var modes = document.createElement('span'); modes.className = 'me-mode-chips';
        var storedMode = deriveMode(config.field, step[config.field]);
        if (storedMode === 'set') lastSetValues[config.field] = step[config.field];
        ['skip', 'set'].forEach(function (mode) {
          var chip = document.createElement('button'); chip.type = 'button';
          chip.className = 'meModeChip'; chip.setAttribute('data-mode', mode);
          chip.setAttribute('aria-pressed', storedMode === mode ? 'true' : 'false');
          chip.textContent = mode.toUpperCase(); modes.appendChild(chip);
        });
        var display = document.createElement('span'); display.className = 'me-servo-value';
        var mode = deriveMode(config.field, step[config.field]);
        display.textContent = mode === 'skip' ? '–' : (mode === 'hold' ? 'HOLD' : String(step[config.field]));
        var slider = document.createElement('input'); slider.type = 'range'; slider.className = 'meSlider hud-range';
        slider.min = '1'; slider.max = '100'; slider.step = String(config.step);
        slider.setAttribute('data-field', config.field); slider.value = mode === 'set' ? String(step[config.field]) : '50';
        slider.disabled = mode !== 'set';
        var number = document.createElement('input'); number.type = 'number'; number.className = 'meNumber hud-input';
        number.setAttribute('inputmode', 'numeric'); number.min = '1'; number.max = '100'; number.step = String(config.step);
        number.setAttribute('data-field', config.field); number.value = mode === 'set' ? String(step[config.field]) : '';
        number.placeholder = mode === 'skip' ? '–' : '';
        number.disabled = mode !== 'set';
        var note = document.createElement('span'); note.className = 'me-clamp-note'; note.setAttribute('aria-live', 'polite');
        row.appendChild(label); row.appendChild(modes); row.appendChild(display);
        row.appendChild(slider); row.appendChild(number); row.appendChild(note); panel.appendChild(row);
        bindServoRow(config, row, step);
      });
      var duplicate = document.createElement('button');
      duplicate.id = 'meDupStep'; duplicate.type = 'button';
      duplicate.className = 'hud-btn hud-btn-secondary'; duplicate.textContent = '⧉ Duplicate position';
      panel.appendChild(duplicate);
    }
    var remove = document.createElement('button');
    remove.id = 'meDelStep'; remove.type = 'button';
    remove.className = 'hud-btn hud-btn-danger'; remove.textContent = '× Delete position';
    panel.appendChild(remove);
  }
  function bindServoRow(config, row, step) {
    var slider = row.querySelector('.meSlider');
    var number = row.querySelector('.meNumber');
    var note = row.querySelector('.me-clamp-note');
    var display = row.querySelector('.me-servo-value');
    slider.min = '1'; slider.max = '100';
    number.min = '1'; number.max = '100';
    var lastValid = deriveMode(config.field, step[config.field]) === 'set' ? Number(step[config.field]) :
      (Object.prototype.hasOwnProperty.call(lastSetValues, config.field) ? lastSetValues[config.field] : 50);
    function updateSummary() {
      if (display) display.textContent = String(step[config.field]);
      var card = el('meTimeline').querySelector('[data-step-index="' + selected.index + '"] .me-step-summary');
      if (card) card.textContent = (selected.index + 1) + ' ▦ ' + stepSummary(step);
      var summary = el('mePosePanel').querySelector('.me-selected-summary');
      if (summary) summary.textContent = stepSummary(step);
    }
    function setValue(value) {
      lastValid = value;
      setStepField(config.field, value);
      slider.value = String(value);
      number.value = String(value);
      updateSummary();
      markDirty();
    }
    slider.addEventListener('input', function () {
      setValue(Number(slider.value));
    });
    number.addEventListener('input', function () {
      if (number.value === '') return;
      var value = Number(number.value);
      if (!isFinite(value) || value < 1 || value > 100) return;
      setValue(value);
    });
    function commitNumber() {
      if (number.value === '' || !isFinite(Number(number.value))) {
        number.value = String(lastValid);
        return;
      }
      var entered = Number(number.value);
      var clamped = Math.max(1, Math.min(100, entered));
      var stepSize = Number(config.step);
      var snapped = Math.max(1, Math.min(100, Math.round((clamped - 1) / stepSize) * stepSize + 1));
      var noteText = entered === 0 ? '0 means SKIP — use the SKIP mode' :
        (snapped !== entered ? 'Value clamped to 1–100.' : '');
      setValue(snapped);
      if (noteText) {
        note.textContent = noteText;
        number.classList.add('me-clamp');
        setTimeout(function () { number.classList.remove('me-clamp'); note.textContent = ''; }, 1500);
      }
    }
    number.addEventListener('change', commitNumber);
    number.addEventListener('blur', commitNumber);
    number.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); commitNumber(); }
    });
    number.addEventListener('focus', function () { number.select(); });
  }
  function addStep(step) {
    if (!draft) return false;
    var steps = draft.entry.steps;
    var nextStep = deepCopy(step === undefined ? DEFAULT_STEP() : step);
    steps.push(nextStep);
    selected.index = steps.length - 1;
    markDirty();
    renderTimeline();
    var card = el('meTimeline').querySelector('[data-step-index="' + selected.index + '"]');
    if (card && card.scrollIntoView) card.scrollIntoView({block: 'nearest'});
    return true;
  }
  function dupStep(index) {
    if (!draft || !draft.entry.steps.length) return false;
    var currentIndex = index === undefined ? selected.index : index;
    var copiedStep = deepCopy(draft.entry.steps[currentIndex]);
    draft.entry.steps.splice(currentIndex + 1, 0, copiedStep);
    selected.index = currentIndex + 1;
    markDirty();
    renderTimeline();
    return true;
  }
  function handlePoseClick(event) {
    var chip = event.target.closest('.meModeChip');
    if (chip) {
      var row = chip.closest('.meServoRow');
      var field = row.getAttribute('data-field');
      var mode = chip.getAttribute('data-mode');
      if (mode === 'skip') setStepField(field, null);
      else setStepField(field,
        Object.prototype.hasOwnProperty.call(lastSetValues, field) ? lastSetValues[field] : 50);
      markDirty();
      renderTimeline();
      return;
    }
    if (event.target.closest('#meDupStep')) dupStep(selected.index);
    else if (event.target.closest('#meDelStep')) delStep(selected.index);
  }
  function showUndoToast(onUndo, message) {
    if (window.showToast) window.showToast(message || 'Position deleted', 'info', 5000);
    var container = el('toastContainer');
    var toast = container && container.lastElementChild;
    if (!toast) return;
    var undoButton = document.createElement('button');
    undoButton.type = 'button';
    undoButton.className = 'me-undo-button';
    undoButton.textContent = 'UNDO';
    var active = true;
    var timeoutId = setTimeout(function () { active = false; }, 5000);
    undoButton.addEventListener('click', function () {
      if (!active) return;
      active = false;
      clearTimeout(timeoutId);
      onUndo();
      toast.remove();
    });
    toast.appendChild(undoButton);
  }
  function delStep(index) {
    if (!draft || !draft.entry.steps.length) return false;
    var steps = draft.entry.steps;
    var removed = steps.splice(index, 1)[0];
    var replacement = null;
    if (!steps.length) {
      replacement = DEFAULT_STEP();
      steps.push(replacement);
    }
    selected.index = Math.min(index, steps.length - 1);
    markDirty();
    renderTimeline();
    showUndoToast(function () {
      if (replacement && steps.length === 1 && steps[0] === replacement) steps.splice(0, 1);
      steps.splice(index, 0, removed);
      selected.index = index;
      markDirty();
      renderTimeline();
    });
    return true;
  }
  function clearBranch(branch) {
    if (!draft || (branch || 'steps') !== 'steps') return false;
    pendingClearSnapshot = deepCopy(draft.entry.steps);
    pendingAction = {type: 'clear-branch'};
    showConfirmStrip('Replace the ' + pendingClearSnapshot.length + ' steps of BASE with one neutral step?', [
      {label: 'Clear', action: 'confirm-clear-branch', danger: true},
      {label: 'Cancel', action: 'cancel'}
    ], true);
    return true;
  }
  function confirmClearBranch() {
    if (!draft || !pendingClearSnapshot) return false;
    var before = pendingClearSnapshot;
    pendingClearSnapshot = null;
    pendingAction = null;
    clearConfirmStrip();
    draft.entry.steps = [DEFAULT_STEP()];
    selected.index = 0;
    markDirty();
    renderTimeline();
    showUndoToast(function () {
      draft.entry.steps = before;
      selected.index = 0;
      markDirty();
      renderTimeline();
    }, 'BASE branch cleared');
    return true;
  }
  function renderTimeline() {
    var timeline = el('meTimeline');
    if (!timeline) return;
    timeline.innerHTML = '';
    var steps = draft && Array.isArray(draft.entry.steps) ? draft.entry.steps : [];
    steps.forEach(function (step, index) {
      var kind = step && step.movement ? 'ref' : 'pose';
      var card = document.createElement('div');
      card.className = 'meStepCard' + (index === selected.index ? ' me-selected' : '');
      card.setAttribute('data-step-index', String(index));
      card.setAttribute('data-kind', kind);
      card.setAttribute('draggable', 'true');
      card.setAttribute('role', 'group');
      card.setAttribute('tabindex', '0');
      card.textContent = '';
      var cardSummary = document.createElement('span'); cardSummary.className = 'me-step-summary';
      cardSummary.textContent = (index + 1) + ' ' + (kind === 'ref' ? '⚡ ' : '▦ ') + stepSummary(step);
      card.appendChild(cardSummary);
      var grip = document.createElement('button');
      grip.type = 'button'; grip.className = 'me-step-grip'; grip.textContent = '⠿';
      grip.setAttribute('aria-label', 'Drag position ' + (index + 1));
      card.appendChild(grip);
      var up = document.createElement('button');
      up.type = 'button'; up.className = 'me-step-move'; up.setAttribute('data-control', 'meStepUp');
      up.setAttribute('aria-label', 'Move position up'); up.textContent = '▲'; card.appendChild(up);
      var down = document.createElement('button');
      down.type = 'button'; down.className = 'me-step-move'; down.setAttribute('data-control', 'meStepDown');
      down.setAttribute('aria-label', 'Move position down'); down.textContent = '▼'; card.appendChild(down);
      var remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'me-step-delete';
      remove.setAttribute('data-action', 'delete-step');
      remove.setAttribute('aria-label', 'Delete position ' + (index + 1));
      remove.textContent = '×';
      card.appendChild(remove);
      timeline.appendChild(card);
    });
    renderSelectedStep();
  }
  function selectStep(index) {
    var steps = draft && Array.isArray(draft.entry.steps) ? draft.entry.steps : [];
    selected.index = Math.max(0, Math.min(index, steps.length - 1));
    renderTimeline();
  }
  function handleTimelineClick(event) {
    var card = event.target.closest('.meStepCard');
    var control = event.target.closest('[data-control]');
    if (control && card) {
      var index = Number(card.getAttribute('data-step-index'));
      if (control.getAttribute('data-control') === 'meStepUp') moveStep(index, index - 1);
      else if (control.getAttribute('data-control') === 'meStepDown') moveStep(index, index + 1);
      return;
    }
    var remove = event.target.closest('[data-action="delete-step"]');
    if (remove && card) { delStep(Number(card.getAttribute('data-step-index'))); return; }
    if (card) selectStep(Number(card.getAttribute('data-step-index')));
  }
  function moveStep(from, to) {
    if (!draft || from < 0 || to < 0 || from >= draft.entry.steps.length || to >= draft.entry.steps.length || from === to) return false;
    var steps = draft.entry.steps;
    var moved = steps.splice(from, 1)[0];
    steps.splice(to, 0, moved);
    selected.index = to;
    markDirty();
    renderTimeline();
    return true;
  }
  function clearDropIndicator() {
    Array.prototype.forEach.call(el('meTimeline').querySelectorAll('.meStepCard'), function (card) {
      card.classList.remove('me-insert-line');
    });
  }
  function handleTimelineDragStart(event) {
    var card = event.target.closest('.meStepCard');
    if (!card) return;
    dragFrom = Number(card.getAttribute('data-step-index'));
    if (event.dataTransfer) event.dataTransfer.setData('text/plain', String(dragFrom));
  }
  function handleTimelineDragOver(event) {
    if (dragFrom === null) return;
    event.preventDefault();
    clearDropIndicator();
    var card = event.target.closest('.meStepCard');
    if (card) card.classList.add('me-insert-line');
  }
  function handleTimelineDrop(event) {
    if (dragFrom === null) return;
    event.preventDefault();
    var card = event.target.closest('.meStepCard');
    var target = card ? Number(card.getAttribute('data-step-index')) : dragFrom;
    var source = dragFrom;
    dragFrom = null;
    clearDropIndicator();
    moveStep(source, target);
  }
  function handleTimelineDragEnd() {
    dragFrom = null;
    clearDropIndicator();
  }
  function handleTimelinePointerCancel() {
    pointerDragFrom = null;
    pointerDragTo = null;
    clearDropIndicator();
  }
  function handleTimelinePointerDown(event) {
    if (!event.target.closest('.me-step-grip')) return;
    event.preventDefault();
    var card = event.target.closest('.meStepCard');
    pointerDragFrom = Number(card.getAttribute('data-step-index'));
    pointerDragTo = pointerDragFrom;
  }
  function handleTimelinePointerMove(event) {
    if (pointerDragFrom === null) return;
    event.preventDefault();
    var target = document.elementFromPoint ? document.elementFromPoint(event.clientX, event.clientY) : event.target;
    var card = target && target.closest('.meStepCard');
    clearDropIndicator();
    if (card) {
      pointerDragTo = Number(card.getAttribute('data-step-index'));
      card.classList.add('me-insert-line');
    }
  }
  function handleTimelinePointerUp(event) {
    if (pointerDragFrom === null) return;
    var target = document.elementFromPoint ? document.elementFromPoint(event.clientX, event.clientY) : event.target;
    var card = target && target.closest('.meStepCard');
    var from = pointerDragFrom;
    var to = card ? Number(card.getAttribute('data-step-index')) : pointerDragTo;
    pointerDragFrom = null;
    pointerDragTo = null;
    clearDropIndicator();
    if (to !== null) moveStep(from, to);
  }
  function openForEdit(id) {
    if (!Object.prototype.hasOwnProperty.call(pool, id)) return false;
    isCreating = false;
    idManuallyEdited = false;
    draft = {id: id, entry: deepCopy(pool[id]), baseSnapshot: deepCopy(pool[id])};
    savedSnapshot = stableStringify(draft.entry);
    selected = {branch: 'steps', index: 0};
    renderEditor();
    return true;
  }
  function openCreate() {
    isCreating = true;
    idManuallyEdited = false;
    draft = {id: '', entry: {
      name: '', type: 'legs_only', description: '', steps: [DEFAULT_STEP()], steps_arms: null
    }, baseSnapshot: null};
    savedSnapshot = stableStringify(draft.entry);
    selected = {branch: 'steps', index: 0};
    renderEditor();
    return true;
  }
  function closeEditor() {
    var pane = el('meEditor');
    if (pane) pane.hidden = true;
    draft = null;
    savedSnapshot = null;
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
  function findReferrers(targetId, entries) {
    var refs = [];
    var sourcePool = entries || pool;
    Object.keys(sourcePool).forEach(function (mid) {
      var entry = sourcePool[mid] || {};
      ['steps', 'steps_arms'].forEach(function (branch) {
        (entry[branch] || []).forEach(function (step, index) {
          if (step && step.movement === targetId) refs.push({mid: mid, branch: branch, stepIndex: index});
        });
      });
    });
    return refs;
  }
  function showConfirmStrip(message, buttons, danger) {
    var strip = el('meConfirmStrip');
    if (!strip) return;
    strip.innerHTML = '';
    strip.hidden = false;
    strip.className = 'me-confirm-strip bld-overwrite-confirm' + (danger ? ' me-confirm-danger' : '');
    var copy = document.createElement('span'); copy.className = 'me-confirm-message'; copy.textContent = message;
    var actions = document.createElement('span'); actions.className = 'me-confirm-actions';
    buttons.forEach(function (config) {
      var button = document.createElement('button'); button.type = 'button';
      button.className = 'hud-btn ' + (config.danger ? 'hud-btn-danger' : 'hud-btn-secondary');
      button.setAttribute('data-action', config.action); button.textContent = config.label;
      actions.appendChild(button);
    });
    strip.appendChild(copy); strip.appendChild(actions);
  }
  function clearConfirmStrip() {
    var strip = el('meConfirmStrip');
    if (!strip) return;
    strip.innerHTML = ''; strip.hidden = true; strip.className = 'me-confirm-strip bld-overwrite-confirm';
  }
  function handleConfirmClick(event) {
    var button = event.target.closest('[data-action]');
    if (!button) return;
    var action = button.getAttribute('data-action');
    if (action === 'cancel' || action === 'cancel-delete') {
      pendingAction = null; pendingClearSnapshot = null; clearConfirmStrip(); return;
    }
    if (action === 'confirm-clear-branch' && pendingAction && pendingAction.type === 'clear-branch') {
      confirmClearBranch(); return;
    }
    if (action === 'confirm-delete' && pendingAction && pendingAction.type === 'delete') {
      var id = pendingAction.id; pendingAction = null; clearConfirmStrip(); performDelete(id); return;
    }
    if (action === 'confirm-reset' && pendingAction && pendingAction.type === 'reset') {
      pendingAction = null; clearConfirmStrip(); performReset(false); return;
    }
    if (action === 'confirm-prune' && pendingAction && pendingAction.type === 'prune' && !pendingAction.hasDanglingRefs) {
      pendingAction = null; clearConfirmStrip(); performReset(true); return;
    }
    if (action === 'prune-anyway' && pendingAction && pendingAction.type === 'prune' && pendingAction.hasDanglingRefs) {
      pendingAction = null; clearConfirmStrip(); performReset(true);
    }
  }
  function deleteMovement(id) {
    var refs = findReferrers(id, pool);
    if (refs.length) {
      var refNames = refs.map(function (ref) {
        var branchName = ref.branch === 'steps_arms' ? 'ARMS' : 'BASE';
        return ref.mid + ' (' + branchName + ' step ' + (ref.stepIndex + 1) + ')';
      }).join(', ');
      showConfirmStrip('"' + id + '" is referenced by ' + refNames + '. Deleting it would make the store invalid — on the next restart ALL custom movements are silently replaced by defaults. Edit the referencing movements to remove the reference first.', [
        {label: 'Cancel', action: 'cancel-delete'}
      ], true);
      return;
    }
    pendingAction = {type: 'delete', id: id};
    if (defaultsOk && Object.prototype.hasOwnProperty.call(defaultsData, id)) {
      showConfirmStrip('"' + id + '" is a shipped movement. The D-pad, gamepad and voice assistant reference it by id — deleting it breaks them silently.', [
        {label: 'Delete anyway', action: 'confirm-delete', danger: true},
        {label: 'Cancel', action: 'cancel-delete'}
      ], true);
      return;
    }
    showConfirmStrip('Delete "' + id + '"?', [
      {label: 'Delete', action: 'confirm-delete'},
      {label: 'Cancel', action: 'cancel-delete'}
    ], false);
  }
  function performDelete(id) {
    return fetch('/movements/' + encodeURIComponent(id), {method: 'DELETE'}).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) return {ok: false, d: d};
        return {ok: true, d: d};
      });
    }).then(function (res) {
      if (!res.ok) {
        showConfirmStrip(res.d.error || 'Delete failed.', [{label: 'Cancel', action: 'cancel-delete'}], true);
        return false;
      }
      clearConfirmStrip(); notify('Deleted "' + id + '"', 'success');
      return fetchPool();
    }).catch(function (error) {
      showConfirmStrip(error && error.message ? error.message : 'Delete failed.', [
        {label: 'Cancel', action: 'cancel-delete'}
      ], true);
      return false;
    });
  }
  function showResetConfirmation() {
    pendingAction = {type: 'reset'};
    showConfirmStrip('Restore every default movement? Edits to shipped movements are lost; custom movements are kept.', [
      {label: 'Restore', action: 'confirm-reset'},
      {label: 'Cancel', action: 'cancel'}
    ], false);
  }
  function showPruneConfirmation() {
    var custom = Object.keys(pool).filter(function (id) {
      return !Object.prototype.hasOwnProperty.call(defaultsData || {}, id);
    });
    var message;
    var referrers = [];
    if (defaultsOk) {
      custom.forEach(function (id) {
        findReferrers(id, defaultsData).forEach(function (ref) {
          ref.targetId = id; referrers.push(ref);
        });
      });
      message = 'Restore ' + Object.keys(defaultsData).length + ' defaults and delete ' +
        custom.length + ' custom movements (' + (custom.join(', ') || 'none') + ')?';
      if (referrers.length) {
        var names = referrers.map(function (ref) {
          var branchName = ref.branch === 'steps_arms' ? 'ARMS' : 'BASE';
          return ref.mid + ' (' + branchName + ' step ' + (ref.stepIndex + 1) + ') references ' + ref.targetId;
        }).join(', ');
        message += ' WARNING: ' + names + ' would be dangling after prune; on the next restart ALL custom movements are silently replaced by defaults.';
      }
    } else {
      message = 'Restore all defaults and delete all custom movements?';
    }
    pendingAction = {type: 'prune', hasDanglingRefs: referrers.length > 0};
    showConfirmStrip(message, [
      {label: referrers.length ? 'Prune anyway' : 'Prune',
        action: referrers.length ? 'prune-anyway' : 'confirm-prune', danger: referrers.length > 0},
      {label: 'Cancel', action: 'cancel'}
    ], referrers.length > 0);
  }
  function performReset(prune) {
    return fetch('/movements/reset', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prune: prune})
    }).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) return {ok: false, d: d};
        return {ok: true, d: d};
      });
    }).then(function (res) {
      if (!res.ok) {
        showConfirmStrip(res.d.error || 'Reset failed.', [{label: 'Cancel', action: 'cancel'}], true);
        return false;
      }
      clearConfirmStrip();
      notify(prune ? 'Movements pruned and defaults restored.' : 'Default movements restored.', 'success');
      return fetchPool();
    }).catch(function (error) {
      showConfirmStrip(error && error.message ? error.message : 'Reset failed.', [
        {label: 'Cancel', action: 'cancel'}
      ], true);
      return false;
    });
  }
  if (el('movements')) el('movements').addEventListener('click', handleLibraryClick);
  if (el('meConfirmStrip')) el('meConfirmStrip').addEventListener('click', handleConfirmClick);
  if (el('meLibSearch')) el('meLibSearch').addEventListener('input', renderLibrary);
  if (el('meLibSort')) el('meLibSort').addEventListener('change', function () {
    selectedSort = el('meLibSort').value || 'id'; renderLibrary();
  });
  if (el('meResetBtn')) el('meResetBtn').addEventListener('click', showResetConfirmation);
  if (el('mePruneBtn')) el('mePruneBtn').addEventListener('click', showPruneConfirmation);
  if (el('meEditor')) {
    el('meEditor').addEventListener('input', handleMetadataInput);
    el('meEditor').addEventListener('change', handleMetadataInput);
  }
  if (el('meTimeline')) {
    el('meTimeline').addEventListener('click', handleTimelineClick);
    el('meTimeline').addEventListener('dragstart', handleTimelineDragStart);
    el('meTimeline').addEventListener('dragover', handleTimelineDragOver);
    el('meTimeline').addEventListener('drop', handleTimelineDrop);
    el('meTimeline').addEventListener('dragend', handleTimelineDragEnd);
    el('meTimeline').addEventListener('pointerdown', handleTimelinePointerDown);
    el('meTimeline').addEventListener('pointermove', handleTimelinePointerMove);
    el('meTimeline').addEventListener('pointerup', handleTimelinePointerUp);
    el('meTimeline').addEventListener('pointercancel', handleTimelinePointerCancel);
  }
  if (el('meAddStep')) el('meAddStep').addEventListener('click', function () { addStep(); });
  if (el('meClearBranch')) el('meClearBranch').addEventListener('click', function () { clearBranch('steps'); });
  if (el('mePosePanel')) el('mePosePanel').addEventListener('click', handlePoseClick);
  fetchPool(); fetchDefaults();




});