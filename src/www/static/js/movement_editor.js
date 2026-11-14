'use strict';
document.addEventListener('DOMContentLoaded', function () {
  var pool = {};
  var defaultsData = null;
  var defaultsOk = false;
  var defaultsFailureNotified = false;
  var pendingAction = null;
  var pendingClearSnapshot = null;
  var pendingGuardAction = null;
  var armsPresent = false;
  var armsStatusKnown = false;
  var armsStatusFailureNotified = false;
  var typeNotice = null;
  var draft = null;
  var playInFlight = false;
  var robotMoving = false;
  var busyTimer = null;
  var busyPollInFlight = false;
  var playRecently = false;
  var pcaAlive = null;
  var savedSnapshot = null;
  var savedFlashTimer = null;
  var rawSnapshot = '';
  var rawPendingAction = null;
  var DRAFT_KEY = 'tars.movementEditor.draft.v1';
  var LIVE_RANGES = {min: 5, max: 100, speedMin: 0.65, speedMax: 1.0};
  var LIVE_LEG_FIELDS = ['left_height', 'right_height', 'left_leg', 'right_leg'];
  var lastLiveLegValues = {left_height: 50, right_height: 50, left_leg: 50, right_leg: 50};
  var liveClampToastShown = false;
  var lastLiveErrorToastAt = -5000;
  var draftTimer = null;
  var draftAutosaveDisabled = false;
  var quotaNoticeShown = false;
  var branch = 'steps';
  var branchState = {
    steps: {index: 0, scrollTop: 0, undoStack: []},
    steps_arms: {index: 0, scrollTop: 0, undoStack: []}
  };
  var selected = branchState[branch];
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
  var STEP_DEFAULTS_MIRROR = {
    left_height: 50, right_height: 50, left_leg: 50, right_leg: 50,
    left_main: null, left_forearm: null, left_hand: null,
    right_main: null, right_forearm: null, right_hand: null,
    speed: 0.85, hold_time: 0, movement: null, arm_speed: null
  };
  var _STEP_KEYS = [
    'left_height', 'right_height', 'left_leg', 'right_leg',
    'left_main', 'left_forearm', 'left_hand', 'right_main', 'right_forearm', 'right_hand',
    'speed', 'hold_time', 'movement', 'arm_speed'
  ];
  var TIMING_FIELDS = [
    {field: 'speed', sliderId: 'meSpeedSlider', numberId: 'meSpeedNumber', label: 'Speed', min: 0, max: 1, step: 0.05, defaultValue: 0.85, caption: 'ramp speed · higher = faster'},
    {field: 'hold_time', sliderId: 'meHoldSlider', numberId: 'meHoldNumber', label: 'Hold', min: 0, max: 10, step: 0.1, defaultValue: 0, caption: 'hold time · seconds'}
  ];
  function el(id) { return document.getElementById(id); }
  function activeBranch() { return branch; }
  function branchSteps(name) {
    if (!draft) return [];
    return draft.entry[name] || [];
  }
  function renderComposerControls() {
    var select = el('meImportSelect');
    if (select) {
      var priorId = select.value;
      select.innerHTML = '';
      var prompt = document.createElement('option'); prompt.value = ''; prompt.textContent = 'Select movement…';
      select.appendChild(prompt);
      Object.keys(pool).filter(function (id) { return !draft || id !== draft.id; }).sort().forEach(function (id) {
        var option = document.createElement('option'); option.value = id; option.textContent = id;
        select.appendChild(option);
      });
      if (priorId && Object.prototype.hasOwnProperty.call(pool, priorId) && (!draft || priorId !== draft.id)) select.value = priorId;
    }
    var label = 'Import into ' + (branch === 'steps' ? 'BASE' : 'ARMS');
    var replace = el('meImportReplace');
    var append = el('meImportAppend');
    if (replace) replace.textContent = label;
    if (append) append.textContent = label + ' (append)';
    var refSelect = el('meRefSelect');
    if (refSelect) {
      var priorRefId = refSelect.value;
      refSelect.innerHTML = '';
      var refPrompt = document.createElement('option'); refPrompt.value = ''; refPrompt.textContent = 'Select movement…';
      refSelect.appendChild(refPrompt);
      Object.keys(pool).filter(function (id) { return !draft || id !== draft.id; }).sort().forEach(function (id) {
        var option = document.createElement('option'); option.value = id; option.textContent = id;
        refSelect.appendChild(option);
      });
      if (priorRefId && Object.prototype.hasOwnProperty.call(pool, priorRefId) && (!draft || priorRefId !== draft.id)) refSelect.value = priorRefId;
    }
  }
  function resetBranchState() {
    typeNotice = null;
    branch = 'steps';
    branchState = {
      steps: {index: 0, scrollTop: 0, undoStack: []},
      steps_arms: {index: 0, scrollTop: 0, undoStack: []}
    };
    selected = branchState[branch];
  }
  function renderBranchBar() {
    ['steps', 'steps_arms'].forEach(function (name) {
      var button = el(name === 'steps' ? 'meBranchBase' : 'meBranchArms');
      if (button) button.setAttribute('aria-pressed', branch === name ? 'true' : 'false');
    });
    var clear = el('meClearBranch');
    if (clear) clear.textContent = 'Clear ' + (branch === 'steps' ? 'BASE' : 'ARMS');
    renderComposerControls();
  }
  function playsNow() {
    if (!draft) return '';
    var armsSteps = Array.isArray(draft.entry.steps_arms) ? draft.entry.steps_arms : [];
    if (armsPresent && armsSteps.length) {
      return 'On this robot: ARMS variant plays (' + armsSteps.length + ' steps)';
    }
    if (armsPresent) return 'On this robot: BASE plays — no arms variant';
    if (draft.entry.type === 'has_arms') return 'Arms not installed: BASE (leg portion) plays';
    return 'On this robot: BASE plays';
  }
  function renderBranchStatus() {
    var status = el('meBranchStatus');
    if (!status) return;
    status.textContent = !draft ? '' : (!armsStatusKnown
      ? 'Arm installation status unavailable.' : playsNow());
  }
  function fetchArmsStatus() {
    return fetch('/get_arms_status').then(function (r) {
      if (!r.ok) throw new Error('Failed to read arm installation status.');
      return r.json();
    }).then(function (d) {
      armsPresent = !!d.arms_present;
      armsStatusKnown = true;
      armsStatusFailureNotified = false;
      renderBranchStatus();
      if (draft) renderSelectedStep();
      return true;
    }).catch(function () {
      armsStatusKnown = false;
      renderBranchStatus();
      if (!armsStatusFailureNotified) {
        armsStatusFailureNotified = true;
        notify('Arm installation status unavailable.', 'error');
      }
      return false;
    });
  }
  function setBranch(name) {
    if (!draft || (name !== 'steps' && name !== 'steps_arms') || name === branch) return false;
    var timeline = el('meTimeline');
    if (timeline) branchState[branch].scrollTop = timeline.scrollTop;
    branch = name;
    selected = branchState[name];
    var steps = branchSteps(name);
    selected.index = steps.length ? Math.max(0, Math.min(selected.index, steps.length - 1)) : 0;
    renderBranchBar();
    renderBranchStatus();
    renderPlayButton();
    renderTimeline();
    if (timeline) timeline.scrollTop = branchState[name].scrollTop;
    return true;
  }
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
      renderComposerControls();
      checkDraftBanner();
      return true;
    }).catch(function () {
      renderError();
      checkDraftBanner();
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
    if (action === 'restore-draft') { restoreDraft(readDraft()); return; }
    if (action === 'discard-draft') { discardDraft(); return; }
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
      refreshBusyUI();
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
    refreshBusyUI();
  }
  function DEFAULT_STEP() {
    return {
      left_height: 50, right_height: 50, left_leg: 50, right_leg: 50,
      left_main: null, left_forearm: null, left_hand: null,
      right_main: null, right_forearm: null, right_hand: null,
      speed: 0.85, hold_time: 0, movement: null, arm_speed: null
    };
  }
  function readDraft() {
    try {
      var raw = window.localStorage.getItem(DRAFT_KEY);
      if (!raw) return null;
      var value = JSON.parse(raw);
      if (!value || value.v !== 1 || !value.entry || typeof value.entry !== 'object') return null;
      return value;
    } catch (error) {
      return null;
    }
  }
  function writeDraft() {
    if (!draft || !isDirty() || draftAutosaveDisabled) return false;
    var value = {
      v: 1,
      id: draft.id || null,
      savedAt: Date.now(),
      entry: deepCopy(draft.entry),
      baseSnapshot: deepCopy(draft.baseSnapshot)
    };
    try {
      window.localStorage.setItem(DRAFT_KEY, JSON.stringify(value));
      return true;
    } catch (error) {
      draftAutosaveDisabled = true;
      if (!quotaNoticeShown) {
        quotaNoticeShown = true;
        var message = error && error.name === 'QuotaExceededError'
          ? 'Draft autosave unavailable (storage quota exceeded); edits remain protected until you leave.'
          : 'Draft autosave unavailable; edits remain protected until you leave.';
        notify(message, 'warning');
      }
      return false;
    }
  }
  function scheduleDraftWrite() {
    if (draftAutosaveDisabled) return;
    if (draftTimer) clearTimeout(draftTimer);
    draftTimer = setTimeout(function () {
      draftTimer = null;
      writeDraft();
    }, 500);
  }
  function clearDraft() {
    if (draftTimer) { clearTimeout(draftTimer); draftTimer = null; }
    try { window.localStorage.removeItem(DRAFT_KEY); }
    catch (error) { /* Storage may be unavailable in private browsing. */ }
    var banner = el('meDraftBanner');
    if (banner) { banner.innerHTML = ''; banner.hidden = true; }
  }
  function showDraftBanner(stored) {
    var banner = el('meDraftBanner');
    if (!banner) return;
    banner.innerHTML = '';
    if (!stored) { banner.hidden = true; return; }
    var name = stored.entry.name || stored.id || 'new movement';
    var line = document.createElement('span');
    line.className = 'me-draft-copy';
    line.textContent = "Unsaved draft for '" + name + "' from " + new Date(stored.savedAt).toLocaleTimeString() + '.';
    var serverEntry = stored.id && pool[stored.id];
    var drift = !!serverEntry && stableStringify(stored.baseSnapshot) !== stableStringify(serverEntry);
    if (drift) line.textContent += ' changed on the robot since this draft was started.';
    var restore = document.createElement('button');
    restore.type = 'button'; restore.className = 'hud-btn hud-btn-primary';
    restore.setAttribute('data-action', 'restore-draft'); restore.textContent = '[Restore]';
    var discard = document.createElement('button');
    discard.type = 'button'; discard.className = 'hud-btn hud-btn-secondary';
    discard.setAttribute('data-action', 'discard-draft'); discard.textContent = '[Discard]';
    banner.appendChild(line); banner.appendChild(restore); banner.appendChild(discard);
    banner.hidden = false;
  }
  function checkDraftBanner() {
    var stored = readDraft();
    showDraftBanner(stored);
  }
  function restoreDraft(stored) {
    if (!stored) return false;
    var serverEntry = stored.id && pool[stored.id] ? pool[stored.id] : null;
    isCreating = !serverEntry;
    idManuallyEdited = isCreating && !!stored.id;
    draft = {id: stored.id || '', entry: deepCopy(stored.entry), baseSnapshot: deepCopy(stored.baseSnapshot)};
    savedSnapshot = serverEntry ? stableStringify(serverEntry) : stableStringify({});
    resetBranchState();
    renderEditor();
    savedSnapshot = serverEntry ? stableStringify(serverEntry) : stableStringify({});
    el('meDraftBanner').hidden = true;
    markDirty();
    return true;
  }
  function discardDraft() {
    clearDraft();
    return true;
  }
  function setSaveState(state) {
    var save = el('meSaveBtn');
    if (!save) return;
    if (savedFlashTimer && state !== 'SAVED') { clearTimeout(savedFlashTimer); savedFlashTimer = null; }
    save.setAttribute('data-state', state);
    var labels = {CLEAN: 'SAVE', DIRTY: 'SAVE', SAVING: 'SAVING…', SAVED: '✓ SAVED', ERROR: 'SAVE'};
    save.textContent = labels[state];
    var validNewId = !isCreating || (draft && draft.id && ID_PATTERN.test(draft.id) && !idTaken(draft.id));
    save.disabled = state === 'CLEAN' || state === 'SAVING' || !validNewId;
  }
  function isDirty() {
    return !!draft && (stableStringify(draft.entry) !== savedSnapshot || (isCreating && !!draft.id));
  }
  function markDirty() {
    var dirty = isDirty();
    var dot = el('meDirtyDot');
    if (dot) dot.hidden = !dirty;
    setSaveState(dirty ? 'DIRTY' : 'CLEAN');
    if (dirty) scheduleDraftWrite();
    else clearDraft();
    renderLibrary();
  }
  function slugify(text) {
    return String(text || '').trim().toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '');
  }
  function idTaken(id) {
    return Object.prototype.hasOwnProperty.call(pool, id);
  }
  function findCycle(startId, entries) {
    var visited = Object.create(null);
    var active = Object.create(null);
    var path = [];
    function visit(id) {
      if (active[id]) return path.concat(id);
      if (visited[id] || !entries[id]) return null;
      active[id] = true;
      path.push(id);
      var cycle = null;
      ['steps', 'steps_arms'].some(function (branchName) {
        var steps = Array.isArray(entries[id][branchName]) ? entries[id][branchName] : [];
        return steps.some(function (step) {
          if (!step || typeof step.movement !== 'string' || !step.movement) return false;
          cycle = visit(step.movement);
          return !!cycle;
        });
      });
      path.pop();
      delete active[id];
      visited[id] = true;
      return cycle;
    }
    return startId ? visit(startId) : null;
  }
  function validateEntry(entry, ctx) {
    ctx = ctx || {};
    entry = entry && typeof entry === 'object' && !Array.isArray(entry) ? entry : {};
    var normalized = deepCopy(entry);
    var errors = [];
    var warnings = [];
    var filledCount = 0;
    var topLevelKeys = ['name', 'type', 'description', 'steps', 'steps_arms'];
    Object.keys(normalized).forEach(function (key) {
      if (topLevelKeys.indexOf(key) === -1) {
        var detail = key === 'id' ? 'id is supplied by the editor' : 'unknown top-level key';
        errors.push({path: 'entry.' + key, msg: detail});
      }
    });
    var id = ctx.id;
    if (!id || !ID_PATTERN.test(id)) errors.push({path: 'id', msg: 'must match ^[a-z0-9_]+$'});
    var existingIds = ctx.existingIds || pool;
    if (ctx.isCreate && id && Object.prototype.hasOwnProperty.call(existingIds, id)) {
      errors.push({path: 'id', msg: 'id exists'});
    }
    if (typeof normalized.name !== 'string' || !normalized.name.trim()) {
      errors.push({path: 'name', msg: 'must be a non-empty string'});
    }
    if (typeof normalized.description !== 'string' || !normalized.description.trim()) {
      errors.push({path: 'description', msg: 'must be a non-empty string'});
    }
    if (normalized.type !== 'legs_only' && normalized.type !== 'has_arms') {
      errors.push({path: 'type', msg: 'must be legs_only or has_arms'});
    }
    if (!Array.isArray(normalized.steps) || normalized.steps.length === 0) {
      errors.push({path: 'steps', msg: 'steps must be a non-empty list'});
    }
    if (!Object.prototype.hasOwnProperty.call(normalized, 'steps_arms')) normalized.steps_arms = null;
    if (normalized.steps_arms !== null && !Array.isArray(normalized.steps_arms)) {
      errors.push({path: 'steps_arms', msg: 'must be a list or null'});
    }
    var legFields = ['left_height', 'right_height', 'left_leg', 'right_leg'];
    var armFields = ['left_main', 'left_forearm', 'left_hand', 'right_main', 'right_forearm', 'right_hand'];
    function validateSteps(branchName, branch) {
      if (!Array.isArray(branch)) return;
      branch.forEach(function (rawStep, index) {
        var path = branchName + '[' + index + ']';
        if (!rawStep || typeof rawStep !== 'object' || Array.isArray(rawStep)) {
          errors.push({path: path, msg: 'must be an object'});
          return;
        }
        var step = deepCopy(rawStep);
        Object.keys(step).forEach(function (key) {
          if (_STEP_KEYS.indexOf(key) === -1) errors.push({path: path + '.' + key, msg: 'unknown field'});
        });
        _STEP_KEYS.forEach(function (key) {
          if (!Object.prototype.hasOwnProperty.call(step, key)) {
            step[key] = deepCopy(STEP_DEFAULTS_MIRROR[key]);
            filledCount++;
          }
        });
        legFields.forEach(function (field) {
          var value = step[field];
          if (value !== null && (typeof value !== 'number' || !isFinite(value) || value < 0 || value > 100)) {
            errors.push({path: path + '.' + field, msg: 'must be null or a number in 0..100'});
          }
        });
        armFields.forEach(function (field) {
          var value = step[field];
          if (value !== null && value !== -1 && (typeof value !== 'number' || !isFinite(value) || value < 0 || value > 100)) {
            errors.push({path: path + '.' + field, msg: 'must be null, -1, or a number in 0..100'});
          }
        });
        if (typeof step.speed !== 'number' || !isFinite(step.speed) || step.speed < 0 || step.speed > 1) {
          errors.push({path: path + '.speed', msg: 'must be a number in 0..1'});
        }
        if (step.arm_speed !== null && (typeof step.arm_speed !== 'number' || !isFinite(step.arm_speed) || step.arm_speed < 0 || step.arm_speed > 1)) {
          errors.push({path: path + '.arm_speed', msg: 'must be null or a number in 0..1'});
        }
        if (typeof step.hold_time !== 'number' || !isFinite(step.hold_time) || step.hold_time < 0) {
          errors.push({path: path + '.hold_time', msg: 'must be a number >= 0'});
        } else if (step.hold_time > 10) {
          warnings.push({path: path + '.hold_time', msg: 'clamped to 10 on save'});
        }
        if (step.movement !== null) {
          if (typeof step.movement !== 'string' || step.movement === id) {
            errors.push({path: path + '.movement', msg: 'must reference another existing movement'});
          } else if (!Object.prototype.hasOwnProperty.call(existingIds, step.movement) && step.movement !== id) {
            errors.push({path: path + '.movement', msg: 'movement does not exist'});
          }
          if (typeof step.movement === 'string' && step.movement && step.hold_time > 0) {
            warnings.push({path: path + '.hold_time', msg: 'stored, not applied at playback'});
          }
        }
        branch[index] = step;
      });
    }
    validateSteps('steps', normalized.steps);
    validateSteps('steps_arms', normalized.steps_arms);
    var cycleEntries = {};
    Object.keys(existingIds).forEach(function (existingId) { cycleEntries[existingId] = existingIds[existingId]; });
    if (id) cycleEntries[id] = normalized;
    var cyclePath = findCycle(id, cycleEntries);
    if (cyclePath && cyclePath.length > 1 && cyclePath[1] !== id) {
      var firstReference = cyclePath[1];
      ['steps', 'steps_arms'].some(function (branchName) {
        var steps = normalized[branchName];
        if (!Array.isArray(steps)) return false;
        return steps.some(function (step, index) {
          if (!step || step.movement !== firstReference) return false;
          errors.push({path: branchName + '[' + index + '].movement', msg: 'reference cycle: ' + cyclePath.join(' → ')});
          return true;
        });
      });
    }
    var notice = filledCount ? 'Filled ' + filledCount + ' missing step fields.' : '';
    return {ok: errors.length === 0, errors: errors, warnings: warnings, filledCount: filledCount, notice: notice, entry: normalized};
  }
  function updateIdStatus() {
    var input = el('meIdInput');
    var error = el('meIdError');
    if (!input || !isCreating) return;
    draft.id = input.value;
    var message = idTaken(draft.id) ? 'id exists' :
      (!ID_PATTERN.test(draft.id) ? 'Use lowercase letters, digits, and underscores.' : '');
    if (error) error.textContent = message;
    setSaveState(el('meSaveBtn').getAttribute('data-state') || 'CLEAN');
  }
  function renderEditor(preserveSaveState) {
    var pane = el('meEditor');
    if (!pane || !draft) return;
    pane.hidden = false;
    rawSnapshot = '';
    setEditorMode('visual');
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
    renderTypeNotice();
    if (!preserveSaveState) {
      el('meDirtyDot').hidden = true;
      setSaveState('CLEAN');
    }
    updateIdStatus();
    renderBranchBar();
    renderBranchStatus();
    renderPlayButton();
    renderTimeline();
    renderOverflowMenu();
  }
  function clampForLive(legs, speed) {
    var safeLegs = {};
    var clamped = false;
    LIVE_LEG_FIELDS.forEach(function (field) {
      var value = Number(legs[field]);
      var safeValue = Math.max(LIVE_RANGES.min, Math.min(LIVE_RANGES.max, value));
      if (safeValue !== value) clamped = true;
      safeLegs[field] = safeValue;
    });
    var numericSpeed = Number(speed);
    var safeSpeed = Math.max(LIVE_RANGES.speedMin, Math.min(LIVE_RANGES.speedMax, numericSpeed));
    if (safeSpeed !== numericSpeed) clamped = true;
    return {legs: safeLegs, speed: safeSpeed, clamped: clamped};
  }
  function showLiveClampNotice() {
    var clampNotice = el('meLiveClampNotice');
    if (clampNotice) clampNotice.hidden = false;
    if (!liveClampToastShown) {
      liveClampToastShown = true;
      notify('Live preview clamped to 5–100 · speed 0.65–1.0', 'warning');
    }
  }
  function notifyLiveError(message) {
    var now = Date.now();
    if (now - lastLiveErrorToastAt < 5000) return;
    lastLiveErrorToastAt = now;
    notify(message, 'error');
  }
  function sendLive(step) {
    var liveToggle = el('meLiveToggle');
    if (!liveToggle || !liveToggle.checked) return false;
    if (!step || step.movement) return false;
    var legs = {};
    LIVE_LEG_FIELDS.forEach(function (field) {
      var value = step[field];
      legs[field] = value === null || value === undefined || value === 0 ? lastLiveLegValues[field] : Number(value);
    });
    var liveValues = clampForLive(legs, step.speed);
    if (liveValues.clamped) showLiveClampNotice();
    return fetch('/move_legs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        left_height: liveValues.legs.left_height,
        right_height: liveValues.legs.right_height,
        left_leg: liveValues.legs.left_leg,
        right_leg: liveValues.legs.right_leg,
        speed: liveValues.speed
      })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error(d.error || 'Live preview failed.');
        return d;
      });
    }).then(function (response) {
      LIVE_LEG_FIELDS.forEach(function (field) { lastLiveLegValues[field] = liveValues.legs[field]; });
      return response;
    }).catch(function (error) {
      notifyLiveError(error && error.message ? error.message : 'Live preview failed.');
      return false;
    });
  }
  function initializeLiveToggle() {
    var liveToggle = el('meLiveToggle');
    if (!liveToggle) return;
    liveToggle.checked = false;
  }
  function forceLiveOff() {
    var toggle = el('meLiveToggle');
    if (toggle) toggle.checked = false;
  }
  function renderPlayButton() {
    var playButton = el('mePlayBtn');
    if (!playButton) return;
    playButton.textContent = playInFlight ? 'PLAYING…' : (branch === 'steps' ? 'PLAY BASE' : 'PLAY ARMS');
    playButton.classList.toggle('me-play-pulse', playInFlight);
    refreshBusyUI();
  }
  function refreshBusyUI() {
    var busyChip = el('meBusyChip');
    if (busyChip) {
      busyChip.hidden = !robotMoving;
      busyChip.textContent = robotMoving ? 'ROBOT BUSY' : '';
      busyChip.classList.toggle('me-busy-active', robotMoving);
    }
    var offlineChip = el('meServoOfflineChip');
    if (offlineChip) offlineChip.hidden = pcaAlive !== false;
    var playButton = el('mePlayBtn');
    if (playButton) playButton.disabled = robotMoving || playInFlight;
    var libraryButtons = el('meLibList') ? el('meLibList').querySelectorAll('[data-action="play"]') : [];
    Array.prototype.forEach.call(libraryButtons, function (button) {
      button.disabled = robotMoving || playInFlight;
    });
  }
  function stopBusyPoll() {
    if (busyTimer !== null) {
      clearTimeout(busyTimer);
      busyTimer = null;
    }
  }
  function scheduleBusyPoll() {
    if (busyTimer !== null) return;
    if (document.hidden || (!playInFlight && !robotMoving && !playRecently)) return;
    busyTimer = setTimeout(pollBusy, 1500);
  }
  function startBusyPoll() {
    if (busyTimer !== null) return;
    if (busyPollInFlight || document.hidden) return;
    if (!playInFlight && !robotMoving && !playRecently) return;
    scheduleBusyPoll();
  }
  function pollBusy() {
    busyTimer = null;
    if (document.hidden) return false;
    if (busyPollInFlight) { scheduleBusyPoll(); return false; }
    busyPollInFlight = true;
    return fetch('/servo_status').then(function (r) {
      if (!r.ok) throw new Error('Failed to read servo status.');
      return r.json();
    }).then(function (status) {
      robotMoving = status.moving === true;
      pcaAlive = status.pca_alive;
      if (!robotMoving && !playInFlight) playRecently = false;
      refreshBusyUI();
      return robotMoving;
    }).catch(function (error) {
      console.warn('Servo status poll failed.', error);
      if (!robotMoving && !playInFlight) playRecently = false;
      return false;
    }).finally(function () {
      busyPollInFlight = false;
      if (document.hidden || (!robotMoving && !playInFlight && !playRecently)) stopBusyPoll();
      else scheduleBusyPoll();
    });
  }
  function handleVisibilityChange() {
    if (document.hidden) {
      forceLiveOff();
      stopBusyPoll();
      return;
    }
    if (playInFlight || robotMoving || playRecently) startBusyPoll();
  }
  function playActiveBranch() {
    if (!draft || playInFlight || robotMoving) return false;
    var playBranch = activeBranch();
    var playSteps = deepCopy(branchSteps(playBranch));
    var validationEntry = {
      name: draft.entry.name || 'Movement',
      type: draft.entry.type,
      description: draft.entry.description || 'Movement',
      steps: deepCopy(playSteps),
      steps_arms: null
    };
    var validation = validateEntry(validationEntry, {
      id: draft.id || 'movement_studio_unsaved',
      existingIds: pool
    });
    var errorStrip = el('meErrorStrip');
    if (errorStrip) { errorStrip.hidden = true; errorStrip.textContent = ''; }
    var timeline = el('meTimeline');
    if (timeline) Array.prototype.forEach.call(timeline.querySelectorAll('.me-card-error'), function (card) {
      card.classList.remove('me-card-error');
    });
    if (!validation.ok) {
      var firstError = validation.errors[0];
      var badStep = firstError && firstError.path.match(/^steps\[(\d+)\]/);
      if (badStep) {
        selected.index = Number(badStep[1]);
        renderTimeline();
        var badCard = timeline && timeline.querySelector('[data-step-index="' + selected.index + '"]');
        if (badCard) badCard.classList.add('me-card-error');
      }
      if (errorStrip) {
        errorStrip.hidden = false;
        errorStrip.textContent = firstError ? firstError.path + ': ' + firstError.msg : 'Playback validation failed.';
      }
      return false;
    }
    playInFlight = true;
    playRecently = true;
    renderPlayButton();
    startBusyPoll();
    var body = JSON.stringify({steps: playSteps});
    return fetch('/play_sequence', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: body
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        return {ok: r.ok, status: r.status, d: d};
      });
    }).then(function (res) {
      if (res.status === 409) {
        robotMoving = true;
        playRecently = true;
        refreshBusyUI();
        notify('Robot is already moving', 'warning');
        startBusyPoll();
        return false;
      }
      if (!res.ok) {
        if (errorStrip) { errorStrip.hidden = false; errorStrip.textContent = res.d.error || 'Playback failed.'; }
        return false;
      }
      return true;
    }).catch(function (error) {
      if (errorStrip) { errorStrip.hidden = false; errorStrip.textContent = error && error.message ? error.message : 'Playback failed.'; }
      return false;
    }).finally(function () {
      playInFlight = false;
      renderPlayButton();
      if (playRecently || robotMoving) startBusyPoll();
    });
  }
  function renderTypeNotice() {
    var container = el('meTypeNotice');
    if (!container) return;
    container.innerHTML = '';
    if (!draft || !typeNotice) return;
    if (typeNotice === 'promotion' && (!Array.isArray(draft.entry.steps_arms) || !draft.entry.steps_arms.length)) {
      var prompt = document.createElement('span'); prompt.textContent = 'Add an arms variant? ';
      var create = document.createElement('button'); create.type = 'button';
      create.className = 'hud-btn hud-btn-secondary'; create.setAttribute('data-action', 'create-arms-from-type');
      create.textContent = 'Create from base';
      container.appendChild(prompt); container.appendChild(create);
    } else if (typeNotice === 'demotion') {
      container.textContent = 'Arms variant kept but unused while type is legs_only.';
    }
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
    else if (target.id === 'meTypeSelect') {
      var previousType = draft.entry.type;
      draft.entry.type = target.value;
      typeNotice = null;
      if (previousType === 'legs_only' && target.value === 'has_arms' &&
          (!Array.isArray(draft.entry.steps_arms) || !draft.entry.steps_arms.length)) typeNotice = 'promotion';
      else if (previousType === 'has_arms' && target.value === 'legs_only' && draft.entry.steps_arms !== null) typeNotice = 'demotion';
    } else return;
    target.classList.toggle('me-field-invalid', !String(target.value).trim());
    renderTypeNotice();
    renderBranchStatus();
    markDirty();
  }
  function selectedStep() {
    var steps = branchSteps(branch);
    return Array.isArray(steps) ? (steps[selected.index] || null) : null;
  }
  function playableSteps(entry) {
    var armsSteps = entry && Array.isArray(entry.steps_arms) ? entry.steps_arms : [];
    if (armsPresent && armsSteps.length) return armsSteps;
    return entry && Array.isArray(entry.steps) ? entry.steps : [];
  }
  function stepSummary(step) {
    if (step && step.movement) {
      var reference = pool[step.movement];
      var count = reference ? playableSteps(reference).length : 0;
      return '⚡ ' + step.movement + ' · plays ' + count + ' steps';
    }
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
    else if (value === -1) step[field] = -1;
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
    if (step.movement) {
      renderRefPanel(step, panel, 'Timing belongs to the referenced movement — this step\'s stored hold/speed are not applied at playback.');
    }
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
      if (armsPresent) {
        var armTitle = document.createElement('h4'); armTitle.textContent = 'ARMS'; panel.appendChild(armTitle);
        var armLegend = document.createElement('p'); armLegend.className = 'me-field-legend';
        armLegend.textContent = 'SKIP = not driven · HOLD = keep last position'; panel.appendChild(armLegend);
        SERVO_FIELDS.filter(function (config) { return config.group === 'arms'; }).forEach(function (config) {
          var row = document.createElement('div'); row.className = 'meServoRow servo-card';
          row.setAttribute('data-field', config.field);
          var label = document.createElement('span'); label.className = 'me-servo-label';
          label.title = config.label; label.textContent = config.code + ' ' + config.label;
          var modes = document.createElement('span'); modes.className = 'me-mode-chips';
          var storedMode = deriveMode(config.field, step[config.field]);
          if (storedMode === 'set') lastSetValues[config.field] = step[config.field];
          var modeOptions = config.group === 'arms' ? ['skip', 'hold', 'set'] : ['skip', 'set'];
          modeOptions.forEach(function (mode) {
            var chip = document.createElement('button'); chip.type = 'button';
            chip.className = 'meModeChip'; chip.setAttribute('data-mode', mode);
            chip.setAttribute('aria-pressed', storedMode === mode ? 'true' : 'false');
            if (mode === 'hold') chip.title = 'Re-poke the last position (keeps this arm energized).';
            chip.textContent = mode.toUpperCase(); modes.appendChild(chip);
          });
          var display = document.createElement('span'); display.className = 'me-servo-value';
          display.textContent = storedMode === 'hold' ? 'HOLD' : (storedMode === 'skip' ? '–' : String(step[config.field]));
          var slider = document.createElement('input'); slider.type = 'range'; slider.className = 'meSlider hud-range';
          slider.min = '1'; slider.max = '100'; slider.step = String(config.step);
          slider.setAttribute('data-field', config.field); slider.value = storedMode === 'set' ? String(step[config.field]) : '50';
          slider.disabled = storedMode !== 'set';
          var number = document.createElement('input'); number.type = 'number'; number.className = 'meNumber hud-input';
          number.setAttribute('inputmode', 'numeric'); number.min = '1'; number.max = '100'; number.step = String(config.step);
          number.setAttribute('data-field', config.field); number.value = storedMode === 'set' ? String(step[config.field]) : '';
          number.placeholder = storedMode === 'hold' ? 'HOLD' : (storedMode === 'skip' ? '–' : '');
          number.disabled = storedMode !== 'set';
          var note = document.createElement('span'); note.className = 'me-clamp-note'; note.setAttribute('aria-live', 'polite');
          row.appendChild(label); row.appendChild(modes); row.appendChild(display);
          row.appendChild(slider); row.appendChild(number); row.appendChild(note); panel.appendChild(row);
          bindServoRow(config, row, step);
        });
      } else {
        var armNotice = document.createElement('p'); armNotice.className = 'me-arms-notice';
        armNotice.textContent = 'Arm servos not installed — arm fields stay editable in RAW mode and are stored, but not played.';
        panel.appendChild(armNotice);
        var armHint = document.createElement('p'); armHint.className = 'me-field-legend';
        armHint.textContent = 'Arm values play on PLAY — live preview is legs-only in this version.';
        panel.appendChild(armHint);
      }
      var timingFooter = document.createElement('div'); timingFooter.className = 'me-timing-footer';
      TIMING_FIELDS.forEach(function (config) { renderTimingField(config, step, timingFooter); });
      renderArmSpeedField(step, timingFooter);
      panel.appendChild(timingFooter);
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
  function renderArmSpeedField(step, parent) {
    var hasArmValue = SERVO_FIELDS.some(function (config) {
      return config.group === 'arms' && step[config.field] !== null && step[config.field] !== undefined;
    });
    var showCustom = branch === 'steps_arms' || hasArmValue;
    var row = document.createElement('div'); row.className = 'me-timing-row me-arm-speed-row';
    var label = document.createElement('label'); label.textContent = 'Arm speed';
    var autoLabel = document.createElement('label'); autoLabel.className = 'me-arm-speed-auto-label';
    var auto = document.createElement('input'); auto.id = 'meArmSpeedAuto'; auto.type = 'checkbox';
    auto.checked = step.arm_speed === null || step.arm_speed === undefined || !showCustom;
    auto.disabled = !showCustom;
    autoLabel.appendChild(auto); autoLabel.appendChild(document.createTextNode('Auto (= speed)'));
    var controls = document.createElement('span'); controls.className = 'me-arm-speed-controls';
    controls.hidden = auto.checked || !showCustom;
    var value = step.arm_speed === null || step.arm_speed === undefined ? Number(step.speed) : Number(step.arm_speed);
    var slider = document.createElement('input'); slider.id = 'meArmSpeedSlider'; slider.type = 'range';
    slider.className = 'meArmSpeedSlider hud-range'; slider.min = '0'; slider.max = '1'; slider.step = '0.05';
    slider.value = String(value);
    var number = document.createElement('input'); number.id = 'meArmSpeedNumber'; number.type = 'number';
    number.className = 'meArmSpeedNumber hud-input'; number.setAttribute('inputmode', 'decimal');
    number.min = '0'; number.max = '1'; number.step = '0.05'; number.value = String(value);
    controls.appendChild(slider); controls.appendChild(number);
    row.appendChild(label); row.appendChild(autoLabel); row.appendChild(controls); parent.appendChild(row);
    bindArmSpeedControls(step, auto, controls, slider, number, showCustom);
  }
  function bindArmSpeedControls(step, auto, controls, slider, number, showCustom) {
    function setValue(value) {
      var clamped = Math.max(0, Math.min(1, Number(value)));
      var snapped = Number((Math.round(clamped / 0.05) * 0.05).toFixed(2));
      step.arm_speed = snapped;
      auto.checked = false;
      controls.hidden = false;
      slider.value = String(snapped);
      number.value = String(snapped);
      markDirty();
    }
    auto.addEventListener('change', function () {
      if (auto.checked) {
        step.arm_speed = null;
        controls.hidden = true;
      } else if (showCustom) {
        var custom = step.arm_speed === null || step.arm_speed === undefined ? Number(step.speed) : Number(step.arm_speed);
        step.arm_speed = custom;
        slider.value = String(custom);
        number.value = String(custom);
        controls.hidden = false;
      } else {
        auto.checked = true;
      }
      markDirty();
    });
    slider.addEventListener('input', function () { setValue(Number(slider.value)); });
    number.addEventListener('input', function () {
      if (number.value === '') return;
      var entered = Number(number.value);
      if (isFinite(entered) && entered >= 0 && entered <= 1) setValue(entered);
    });
    function commitNumber() {
      if (number.value === '' || !isFinite(Number(number.value))) {
        number.value = String(step.arm_speed === null || step.arm_speed === undefined ? step.speed : step.arm_speed);
        return;
      }
      setValue(number.value);
    }
    number.addEventListener('change', commitNumber);
    number.addEventListener('blur', commitNumber);
  }
  function refSummary(id) {
    var entry = pool[id];
    var steps = entry ? playableSteps(entry) : [];
    var hold = 0;
    steps.forEach(function (step) {
      var seconds = Number(step && step.hold_time);
      if (isFinite(seconds)) hold += Math.min(10, Math.max(0, seconds));
    });
    return {id: id, name: entry && entry.name ? entry.name : id, steps: steps.length, hold: hold};
  }
  function renderRefPanel(step, panel, caption) {
    var summary = refSummary(step.movement);
    var details = document.createElement('p'); details.className = 'me-ref-summary';
    details.textContent = summary.id + ' — ' + summary.name + ' · ' + summary.steps + ' steps · ' + summary.hold.toFixed(1) + 's total hold';
    panel.appendChild(details);
    var open = document.createElement('button'); open.type = 'button';
    open.className = 'hud-btn hud-btn-secondary me-ref-open'; open.setAttribute('data-action', 'open-ref');
    open.setAttribute('data-ref-id', step.movement); open.textContent = 'open ↗';
    panel.appendChild(open);
    var refNote = document.createElement('p'); refNote.className = 'me-ref-note';
    refNote.textContent = caption; panel.appendChild(refNote);
  }
  function renderTimingField(config, step, parent) {
    var row = document.createElement('div'); row.className = 'me-timing-row';
    var label = document.createElement('label'); label.textContent = config.label;
    var caption = document.createElement('small'); caption.textContent = config.caption;
    var current = step[config.field];
    var value = current === null || current === undefined ? config.defaultValue : Number(current);
    var slider = document.createElement('input'); slider.id = config.sliderId; slider.type = 'range';
    slider.className = 'meTimingSlider hud-range'; slider.min = String(config.min); slider.max = String(config.max);
    slider.step = String(config.step); slider.value = String(value);
    var number = document.createElement('input'); number.id = config.numberId; number.type = 'number';
    number.className = 'meTimingNumber hud-input'; number.setAttribute('inputmode', 'decimal');
    number.min = String(config.min); number.max = String(config.max); number.step = String(config.step);
    number.value = String(value);
    var note = document.createElement('span'); note.className = 'me-clamp-note'; note.setAttribute('aria-live', 'polite');
    row.appendChild(label); row.appendChild(slider); row.appendChild(number); row.appendChild(caption); row.appendChild(note);
    parent.appendChild(row);
    bindTimingControl(config, row, step);
  }
  function bindTimingControl(config, row, step) {
    var slider = row.querySelector('.meTimingSlider');
    var number = row.querySelector('.meTimingNumber');
    var note = row.querySelector('.me-clamp-note');
    var lastValid = Number(step[config.field]);
    function setValue(value) {
      lastValid = value;
      step[config.field] = value;
      slider.value = String(value);
      number.value = String(value);
      updateStepSummary(step);
      markDirty();
    }
    slider.addEventListener('input', function () { setValue(Number(slider.value)); });
    number.addEventListener('input', function () {
      if (number.value === '') return;
      var value = Number(number.value);
      if (!isFinite(value) || value < config.min || value > config.max) return;
      setValue(value);
    });
    function commitNumber() {
      if (number.value === '' || !isFinite(Number(number.value))) { number.value = String(lastValid); return; }
      var entered = Number(number.value);
      var clamped = Math.max(config.min, Math.min(config.max, entered));
      var snapped = Number((Math.round((clamped - config.min) / config.step) * config.step + config.min).toFixed(3));
      var noteText = config.field === 'hold_time' && entered > 10 ? 'Hold max is 10 s' :
        (snapped !== entered ? 'Value clamped to ' + config.min + '–' + config.max + '.' : '');
      setValue(snapped);
      if (noteText) {
        note.textContent = noteText; number.classList.add('me-clamp');
        setTimeout(function () { number.classList.remove('me-clamp'); note.textContent = ''; }, 1500);
      }
    }
    number.addEventListener('change', commitNumber);
    number.addEventListener('blur', commitNumber);
    number.addEventListener('keydown', function (event) { if (event.key === 'Enter') { event.preventDefault(); commitNumber(); } });
    number.addEventListener('focus', function () { number.select(); });
  }
  function updateStepSummary(step) {
    var card = el('meTimeline').querySelector('[data-step-index="' + selected.index + '"] .me-step-summary');
    if (card) card.textContent = (selected.index + 1) + ' ▦ ' + stepSummary(step);
    var summary = el('mePosePanel').querySelector('.me-selected-summary');
    if (summary) summary.textContent = stepSummary(step);
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
    var lastCommittedNumber = number.value;
    var lastCommittedSlider = slider.value;
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
    slider.addEventListener('change', function () {
      if (config.group === 'legs' && String(slider.value) !== lastCommittedSlider) sendLive(step);
      lastCommittedSlider = String(slider.value);
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
      if (config.group === 'legs' && String(snapped) !== lastCommittedNumber) sendLive(step);
      lastCommittedNumber = String(snapped);
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
    if (!Array.isArray(draft.entry[branch])) draft.entry[branch] = [];
    var steps = branchSteps(branch);
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
    var steps = branchSteps(branch);
    if (!draft || !steps.length) return false;
    var currentIndex = index === undefined ? selected.index : index;
    var copiedStep = deepCopy(steps[currentIndex]);
    steps.splice(currentIndex + 1, 0, copiedStep);
    selected.index = currentIndex + 1;
    markDirty();
    renderTimeline();
    return true;
  }
  function addRef(id) {
    if (!draft || !id || id === draft.id || !Object.prototype.hasOwnProperty.call(pool, id)) return false;
    var step = {
      left_height: null, right_height: null, left_leg: null, right_leg: null,
      left_main: null, left_forearm: null, left_hand: null,
      right_main: null, right_forearm: null, right_hand: null,
      speed: 0.85, hold_time: 0, movement: id, arm_speed: null
    };
    if (!Array.isArray(draft.entry[branch])) draft.entry[branch] = [];
    var steps = branchSteps(branch);
    steps.push(step);
    selected.index = steps.length - 1;
    markDirty();
    renderTimeline();
    return true;
  }
  function handlePoseClick(event) {
    var open = event.target.closest('[data-action="open-ref"]');
    if (open) { openForEdit(open.getAttribute('data-ref-id')); return; }
    var chip = event.target.closest('.meModeChip');
    if (chip) {
      var row = chip.closest('.meServoRow');
      var field = row.getAttribute('data-field');
      var liveStep = selectedStep();
      var mode = chip.getAttribute('data-mode');
      if (mode === 'skip') setStepField(field, null);
      else if (mode === 'hold') setStepField(field, -1);
      else setStepField(field,
        Object.prototype.hasOwnProperty.call(lastSetValues, field) ? lastSetValues[field] : 50);
      markDirty();
      renderTimeline();
      if (LIVE_LEG_FIELDS.indexOf(field) !== -1) sendLive(liveStep);
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
    var branchName = branch;
    var steps = branchSteps(branchName);
    if (!draft || !steps.length) return false;
    var removed = steps.splice(index, 1)[0];
    var replacement = null;
    if (!steps.length) {
      replacement = DEFAULT_STEP();
      steps.push(replacement);
    }
    selected.index = Math.min(index, steps.length - 1);
    var undoRecord = {removed: removed};
    branchState[branchName].undoStack.push(undoRecord);
    markDirty();
    renderTimeline();
    showUndoToast(function () {
      var stack = branchState[branchName].undoStack;
      var recordIndex = stack.indexOf(undoRecord);
      if (recordIndex !== -1) stack.splice(recordIndex, 1);
      if (replacement && steps.length === 1 && steps[0] === replacement) steps.splice(0, 1);
      steps.splice(index, 0, removed);
      branchState[branchName].index = index;
      if (branchName === branch) selected = branchState[branch];
      markDirty();
      renderTimeline();
    });
    return true;
  }
  function clearBranch(branchName) {
    var branch = branchName || activeBranch();
    var steps = branchSteps(branch);
    if (!draft || (branch !== 'steps' && branch !== 'steps_arms') || !steps.length) return false;
    pendingClearSnapshot = {branch: branch, steps: deepCopy(steps)};
    pendingAction = {type: 'clear-branch'};
    var label = branch === 'steps' ? 'BASE' : 'ARMS';
    showConfirmStrip('Replace the ' + pendingClearSnapshot.steps.length + ' steps of ' + label + ' with one neutral step?', [
      {label: 'Clear', action: 'confirm-clear-branch', danger: true},
      {label: 'Cancel', action: 'cancel'}
    ], true);
    return true;
  }
  function confirmClearBranch() {
    if (!draft || !pendingClearSnapshot) return false;
    var branchName = pendingClearSnapshot.branch;
    var before = pendingClearSnapshot.steps;
    var undoRecord = {steps: before};
    pendingClearSnapshot = null;
    pendingAction = null;
    clearConfirmStrip();
    branchState[branchName].undoStack.push(undoRecord);
    if (branchName === 'steps') draft.entry.steps = [DEFAULT_STEP()];
    else draft.entry[branchName] = [DEFAULT_STEP()];
    branchState[branchName].index = 0;
    if (branchName === branch) selected = branchState[branch];
    markDirty();
    renderTimeline();
    var label = branchName === 'steps' ? 'BASE' : 'ARMS';
    showUndoToast(function () {
      var stack = branchState[branchName].undoStack;
      var recordIndex = stack.indexOf(undoRecord);
      if (recordIndex !== -1) stack.splice(recordIndex, 1);
      if (branchName === 'steps') draft.entry.steps = before;
      else draft.entry[branchName] = before;
      branchState[branchName].index = 0;
      if (branchName === branch) selected = branchState[branch];
      markDirty();
      renderTimeline();
    }, label + ' branch cleared');
    return true;
  }
  function createArmsFromBase() {
    if (!draft) return false;
    draft.entry.steps_arms = deepCopy(draft.entry.steps);
    branchState.steps_arms.index = Math.min(branchState.steps_arms.index, draft.entry.steps_arms.length - 1);
    markDirty();
    renderBranchStatus();
    renderTimeline();
    return true;
  }
  function startArmsEmpty() {
    if (!draft) return false;
    draft.entry.steps_arms = [DEFAULT_STEP()];
    branchState.steps_arms.index = 0;
    markDirty();
    renderBranchStatus();
    renderTimeline();
    return true;
  }
  function importIntoBranch(kind) {
    if (!draft || (kind !== 'replace' && kind !== 'append')) return false;
    var select = el('meImportSelect');
    var sourceId = select && select.value;
    var sourceEntry = sourceId && pool[sourceId];
    if (!sourceEntry) { notify('Choose a movement to import.', 'warning'); return false; }
    var sourceSteps = sourceEntry[branch];
    if (!Array.isArray(sourceSteps) || !sourceSteps.length) {
      notify('The selected movement has no ' + (branch === 'steps' ? 'BASE' : 'ARMS') + ' steps.', 'warning');
      return false;
    }
    var target = branchSteps(branch);
    var branchLabel = branch === 'steps' ? 'BASE' : 'ARMS';
    var action = {type: 'import-branch', kind: kind, branch: branch, sourceId: sourceId};
    if (kind === 'replace' && target.length) {
      action.targetCount = target.length;
      showConfirmStrip('Replace ' + target.length + ' ' + branchLabel + ' steps with ' + sourceId + ' (' + sourceSteps.length + ' steps)?', [
        {label: 'Replace', action: 'confirm-import-branch', danger: true},
        {label: 'Cancel', action: 'cancel'}
      ], true);
      pendingAction = action;
      return true;
    }
    return applyBranchImport(action);
  }
  function applyBranchImport(action) {
    if (!draft || !action || !pool[action.sourceId]) return false;
    var sourceEntry = pool[action.sourceId];
    var sourceSteps = sourceEntry[action.branch];
    if (!Array.isArray(sourceSteps) || !sourceSteps.length) return false;
    var imported = deepCopy(sourceSteps);
    var target = Array.isArray(draft.entry[action.branch]) ? draft.entry[action.branch] : [];
    if (action.kind === 'replace') {
      draft.entry[action.branch] = deepCopy(sourceSteps);
      branchState[action.branch].index = Math.min(branchState[action.branch].index, imported.length - 1);
      if (branch === action.branch) selected = branchState[branch];
    } else if (action.kind === 'append') {
      draft.entry[action.branch] = target.concat(imported);
    } else return false;
    clearConfirmStrip();
    pendingAction = null;
    markDirty();
    renderBranchStatus();
    if (branch === action.branch) renderTimeline();
    return true;
  }
  function removeArmsVariant() {
    if (!draft || !Array.isArray(draft.entry.steps_arms) || !draft.entry.steps_arms.length) return false;
    draft.entry.steps_arms = null;
    branchState.steps_arms.index = 0;
    branchState.steps_arms.scrollTop = 0;
    if (branch === 'steps_arms') selected = branchState.steps_arms;
    clearConfirmStrip();
    markDirty();
    renderBranchStatus();
    renderTimeline();
    return true;
  }
  function showRemoveArmsVariantConfirm() {
    if (!draft || !Array.isArray(draft.entry.steps_arms) || !draft.entry.steps_arms.length) return false;
    pendingAction = {type: 'remove-arms-variant'};
    showConfirmStrip('Delete the ' + draft.entry.steps_arms.length + '-step arms variant? BASE is kept.', [
      {label: 'Remove', action: 'confirm-remove-arms-variant', danger: true},
      {label: 'Cancel', action: 'cancel'}
    ], true);
    return true;
  }
  function renderTimeline() {
    var timeline = el('meTimeline');
    if (!timeline) return;
    timeline.innerHTML = '';
    var steps = branchSteps(branch);
    if (branch === 'steps_arms' && (!Array.isArray(draft.entry.steps_arms) || draft.entry.steps_arms.length === 0)) {
      var empty = document.createElement('div'); empty.className = 'me-empty-variant';
      var message = document.createElement('p'); message.textContent = 'No ARMS variant yet.'; empty.appendChild(message);
      var create = document.createElement('button'); create.type = 'button';
      create.className = 'hud-btn hud-btn-primary'; create.setAttribute('data-action', 'create-arms-from-base');
      create.textContent = 'Create from BASE'; empty.appendChild(create);
      var start = document.createElement('button'); start.type = 'button';
      start.className = 'hud-btn hud-btn-secondary'; start.setAttribute('data-action', 'start-empty-arms');
      start.textContent = 'Start empty'; empty.appendChild(start);
      timeline.appendChild(empty);
      renderSelectedStep();
      return;
    }
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
    var steps = branchSteps(branch);
    selected.index = steps.length ? Math.max(0, Math.min(index, steps.length - 1)) : 0;
    renderTimeline();
  }
  function handleTimelineClick(event) {
    var action = event.target.closest('[data-action]');
    if (action && action.getAttribute('data-action') === 'create-arms-from-base') { createArmsFromBase(); return; }
    if (action && action.getAttribute('data-action') === 'start-empty-arms') { startArmsEmpty(); return; }
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
    var steps = branchSteps(branch);
    if (!draft || from < 0 || to < 0 || from >= steps.length || to >= steps.length || from === to) return false;
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
  function rawEntryFromDraft() {
    var rawEntry = {};
    ['name', 'type', 'description', 'steps', 'steps_arms'].forEach(function (key) {
      rawEntry[key] = deepCopy(draft.entry[key]);
    });
    return rawEntry;
  }
  function renderRaw() {
    if (!draft) return;
    rawSnapshot = JSON.stringify(rawEntryFromDraft(), null, 2);
    el('meRawText').value = rawSnapshot;
    el('meRawNotice').textContent = '';
  }
  function rawDirty() {
    var text = el('meRawText');
    return editorMode === 'raw' && !!text && text.value !== rawSnapshot;
  }
  function setEditorMode(mode) {
    editorMode = mode;
    var pane = el('meEditor');
    if (pane) pane.classList.toggle('me-raw-mode', mode === 'raw');
    var workspace = pane && pane.querySelector('.me-editor-workspace');
    if (workspace) workspace.hidden = mode === 'raw';
    var rawPanel = el('meRawPanel');
    if (rawPanel) rawPanel.hidden = mode !== 'raw';
    if (el('meModeVisual')) el('meModeVisual').setAttribute('aria-pressed', mode === 'visual' ? 'true' : 'false');
    if (el('meModeRaw')) el('meModeRaw').setAttribute('aria-pressed', mode === 'raw' ? 'true' : 'false');
  }
  function showRawDirtyPrompt(action) {
    rawPendingAction = action;
    showConfirmStrip('Apply or discard JSON changes first.', [
      {label: 'Apply', action: 'raw-apply-pending'},
      {label: 'Discard edits', action: 'raw-discard-pending'}
    ], false);
  }
  function handleBranchClick(event) {
    var createVariant = event.target.closest('[data-action="create-arms-from-type"]');
    if (createVariant) {
      typeNotice = null;
      createArmsFromBase();
      renderTypeNotice();
      return;
    }
    var button = event.target.closest('#meBranchBase, #meBranchArms');
    if (button) setBranch(button.getAttribute('data-branch'));
  }
  function handleModeClick(event) {
    if (event.target.closest('#meRawApply')) {
      applyRaw(el('meRawText').value);
      return;
    }
    if (event.target.closest('#meModeRaw')) {
      if (editorMode !== 'raw') { renderRaw(); setEditorMode('raw'); }
      return;
    }
    if (event.target.closest('#meModeVisual') && editorMode === 'raw') {
      if (rawDirty()) { showRawDirtyPrompt('visual'); return; }
      setEditorMode('visual');
    }
  }
  function applyRaw(text) {
    var parsed;
    try { parsed = JSON.parse(text); }
    catch (error) {
      var syntaxStrip = el('meErrorStrip');
      syntaxStrip.hidden = false;
      syntaxStrip.textContent = 'JSON: ' + error.message;
      return {ok: false, errors: [{path: 'JSON', msg: error.message}]};
    }
    var validation = validateEntry(parsed, {id: draft.id, isCreate: isCreating, existingIds: pool});
    if (!validation.ok) {
      var errorStrip = el('meErrorStrip');
      errorStrip.hidden = false;
      errorStrip.textContent = validation.errors.slice(0, 5).map(function (error) {
        return error.path + ': ' + error.msg;
      }).join('\n');
      return validation;
    }
    draft.entry = validation.entry;
    el('meNameInput').value = draft.entry.name || '';
    el('meTypeSelect').value = draft.entry.type || 'legs_only';
    el('meDescInput').value = draft.entry.description || '';
    var notices = [];
    if (validation.notice) notices.push(validation.notice);
    validation.warnings.forEach(function (warning) { notices.push(warning.path + ': ' + warning.msg); });
    el('meRawNotice').textContent = notices.join(' ');
    el('meErrorStrip').hidden = true;
    rawPendingAction = null;
    setEditorMode('visual');
    markDirty();
    renderTimeline();
    return validation;
  }
  function saveEntry() {
    if (!draft || el('meSaveBtn').getAttribute('data-state') === 'SAVING') return false;
    var rawSave = editorMode === 'raw';
    if (rawSave) {
      if (rawDirty()) { showRawDirtyPrompt('save'); return false; }
      var rawResult = applyRaw(el('meRawText').value);
      if (!rawResult.ok) return false;
    }
    if (!rawSave && Array.isArray(draft.entry.steps_arms) && draft.entry.steps_arms.length === 0) {
      draft.entry.steps_arms = null;
      notify('Empty arms variant removed', 'info');
    }
    var validation = validateEntry(draft.entry, {id: draft.id, isCreate: isCreating, existingIds: pool});
    var strip = el('meErrorStrip');
    if (!validation.ok) {
      strip.hidden = false;
      strip.textContent = validation.errors.slice(0, 5).map(function (error) {
        return error.path + ': ' + error.msg;
      }).join('\n');
      return false;
    }
    draft.entry = validation.entry;
    if (validation.warnings.length) {
      strip.hidden = false;
      strip.textContent = validation.warnings.slice(0, 5).map(function (warning) {
        return warning.path + ': ' + warning.msg;
      }).join('\n');
    }
    var wasCreate = isCreating;
    var savedId = draft.id;
    var payload = {
      name: draft.entry.name,
      type: draft.entry.type,
      description: draft.entry.description,
      steps: deepCopy(draft.entry.steps),
      steps_arms: deepCopy(draft.entry.steps_arms)
    };
    if (wasCreate) payload.id = savedId;
    setSaveState('SAVING');
    var request = wasCreate ? fetch('/movements', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    }) : fetch('/movements/' + encodeURIComponent(savedId), {
      method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    });
    return request.then(function (r) {
      return r.json().then(function (d) { return {ok: r.ok, d: d}; });
    }).then(function (res) {
      if (!res.ok) {
        setSaveState('ERROR');
        strip.hidden = false;
        strip.textContent = res.d.error;
        if (!strip.textContent) strip.textContent = 'Save failed.';
        notify(res.d.error || 'Save failed.', 'error');
        return false;
      }
      isCreating = false;
      idManuallyEdited = false;
      draft.id = savedId;
      savedSnapshot = stableStringify(draft.entry);
      draft.baseSnapshot = deepCopy(draft.entry);
      clearDraft();
      strip.hidden = true;
      strip.textContent = '';
      renderEditor(true);
      el('meDirtyDot').hidden = true;
      setSaveState('SAVED');
      savedFlashTimer = setTimeout(function () {
        if (el('meSaveBtn').getAttribute('data-state') === 'SAVED') setSaveState('CLEAN');
        savedFlashTimer = null;
      }, 1500);
      notify('Saved "' + draft.entry.name + '"', 'success');
      fetchPool();
      return true;
    }).catch(function (error) {
      setSaveState('ERROR');
      strip.hidden = false;
      strip.textContent = error && error.message ? error.message : 'Save failed.';
      notify(strip.textContent, 'error');
      return false;
    });
  }
  function meConfirmDiscard(action) {
    if (!draft || (!isDirty() && !rawDirty())) { action(); return true; }
    pendingGuardAction = action;
    var name = draft.entry.name || draft.id || 'new movement';
    showConfirmStrip('Discard unsaved changes to "' + name + '"?', [
      {label: 'Discard', action: 'discard-unsaved', danger: true},
      {label: 'Keep editing', action: 'keep-editing'}
    ], true);
    return false;
  }
  function discardCurrentEdits() {
    if (draft) {
      var savedEntry = draft.id && pool[draft.id] ? deepCopy(pool[draft.id]) :
        (draft.baseSnapshot ? deepCopy(draft.baseSnapshot) : null);
      if (savedEntry) {
        draft.entry = savedEntry;
        isCreating = false;
      } else {
        draft.id = '';
        isCreating = true;
        draft.entry = {name: '', type: 'legs_only', description: '', steps: [DEFAULT_STEP()], steps_arms: null};
      }
      savedSnapshot = stableStringify(draft.entry);
      if (el('meRawText')) el('meRawText').value = rawSnapshot;
      rawSnapshot = '';
      el('meDirtyDot').hidden = true;
      setSaveState('CLEAN');
      renderTimeline();
    }
    clearDraft();
  }
  function handleBeforeUnload(event) {
    if (draft && (isDirty() || rawDirty())) {
      event.preventDefault();
      event.returnValue = '';
      return '';
    }
  }
  function openForEdit(id, discarded) {
    if (!discarded && draft && (draft.id !== id || isDirty() || rawDirty())) {
      return meConfirmDiscard(function () { openForEdit(id, true); });
    }
    if (!Object.prototype.hasOwnProperty.call(pool, id)) return false;
    isCreating = false;
    idManuallyEdited = false;
    draft = {id: id, entry: deepCopy(pool[id]), baseSnapshot: deepCopy(pool[id])};
    savedSnapshot = stableStringify(draft.entry);
    resetBranchState();
    renderEditor();
    return true;
  }
  function openCreate(discarded) {
    if (!discarded && draft && (isDirty() || rawDirty())) {
      return meConfirmDiscard(function () { openCreate(true); });
    }
    isCreating = true;
    idManuallyEdited = false;
    draft = {id: '', entry: {
      name: '', type: 'legs_only', description: '', steps: [DEFAULT_STEP()], steps_arms: null
    }, baseSnapshot: null};
    savedSnapshot = stableStringify(draft.entry);
    resetBranchState();
    renderEditor();
    return true;
  }
  function closeEditor(discarded) {
    if (!discarded && draft && (isDirty() || rawDirty())) {
      return meConfirmDiscard(function () { closeEditor(true); });
    }
    forceLiveOff();
    var pane = el('meEditor');
    if (pane) pane.hidden = true;
    clearDraft();
    draft = null;
    savedSnapshot = null;
    return true;
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
      renderOverflowMenu();
      return true;
    }).catch(function () {
      defaultsData = null;
      defaultsOk = false;
      applyBadges();
      renderOverflowMenu();
      if (!defaultsFailureNotified) {
        defaultsFailureNotified = true;
        notify('Movement defaults unavailable; status badges are hidden.', 'error');
      }
      return false;
    });
  }
  function playMovement(id, button) {
    if ((button && button.disabled) || playInFlight || robotMoving) return false;
    playInFlight = true;
    playRecently = true;
    if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
    refreshBusyUI();
    startBusyPoll();
    return fetch('/play_saved_sequence', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: id})
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (d) { return {ok: false, status: r.status, d: d}; });
      return r.json().then(function (d) { return {ok: true, status: r.status, d: d}; });
    }).then(function (res) {
      if (res.status === 409) {
        robotMoving = true;
        playRecently = true;
        refreshBusyUI();
        notify('Robot is already moving', 'warning');
        startBusyPoll();
      } else if (!res.ok) notify(res.d.error || 'Playback failed.', 'error');
      else notify('Played "' + id + '"', 'success');
    }).catch(function (error) {
      notify(error && error.message ? error.message : 'Playback failed.', 'error');
    }).finally(function () {
      playInFlight = false;
      if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
      refreshBusyUI();
      if (playRecently || robotMoving) startBusyPoll();
    });
  }
  function deepCopy(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }
  function duplicateMovement(id, button, discarded, openAfter) {
    if (!discarded && draft && draft.id === id && (isDirty() || rawDirty())) {
      return meConfirmDiscard(function () { duplicateMovement(id, button, true, openAfter); });
    }
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
      return fetchPool().then(function () {
        if (openAfter && Object.prototype.hasOwnProperty.call(pool, newId)) openForEdit(newId, true);
        return true;
      });
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
  function shouldShowRestoreDefault() {
    return !!(draft && !isCreating && defaultsOk && defaultsData &&
      Object.prototype.hasOwnProperty.call(defaultsData, draft.id) &&
      stableStringify(draft.entry) !== stableStringify(defaultsData[draft.id]));
  }
  function renderOverflowMenu() {
    var menu = el('meOverflowMenu');
    if (!menu) return;
    menu.innerHTML = '';
    var actions = {
      duplicate: 'Duplicate',
      delete: 'Delete',
      'restore-default': 'Restore default',
      'remove-arms-variant': 'Remove arms variant',
      close: 'Close'
    };
    var visible = draft && draft.id && !isCreating ? ['duplicate', 'delete'] : [];
    if (shouldShowRestoreDefault()) visible.push('restore-default');
    if (draft && Array.isArray(draft.entry.steps_arms) && draft.entry.steps_arms.length) visible.push('remove-arms-variant');
    if (draft) visible.push('close');
    visible.forEach(function (action) {
      var item = document.createElement('button');
      item.type = 'button'; item.setAttribute('role', 'menuitem');
      item.setAttribute('data-action', action); item.textContent = actions[action];
      menu.appendChild(item);
    });
  }
  function handleOverflowClick(event) {
    if (event.target.closest('#meOverflowBtn')) {
      var menu = el('meOverflowMenu');
      if (menu.hidden) renderOverflowMenu();
      menu.hidden = !menu.hidden;
      el('meOverflowBtn').setAttribute('aria-expanded', menu.hidden ? 'false' : 'true');
      return;
    }
    var item = event.target.closest('#meOverflowMenu [data-action]');
    if (!item || !draft) return;
    var action = item.getAttribute('data-action');
    el('meOverflowMenu').hidden = true;
    el('meOverflowBtn').setAttribute('aria-expanded', 'false');
    if (action === 'duplicate') duplicateMovement(draft.id, null, false, true);
    else if (action === 'delete') deleteMovement(draft.id);
    else if (action === 'restore-default') showRestoreDefaultConfirm(draft.id);
    else if (action === 'remove-arms-variant') showRemoveArmsVariantConfirm();
    else if (action === 'close') closeEditor();
  }
  function handleOverflowOutsideClick(event) {
    var menu = el('meOverflowMenu');
    if (!menu || menu.hidden) return;
    if (!event.target.closest('#meOverflowBtn') && !event.target.closest('#meOverflowMenu')) {
      menu.hidden = true;
      el('meOverflowBtn').setAttribute('aria-expanded', 'false');
    }
  }
  function showRestoreDefaultConfirm(id) {
    if (!shouldShowRestoreDefault() || id !== draft.id) return false;
    pendingAction = {type: 'restore-default', id: id};
    showConfirmStrip("Restore '" + draft.entry.name + "' to the shipped default? Your edits are lost.", [
      {label: 'Restore', action: 'confirm-restore-default', danger: true},
      {label: 'Cancel', action: 'cancel'}
    ], true);
    return true;
  }
  function restoreDefault(id) {
    if (!defaultsOk || !defaultsData || !Object.prototype.hasOwnProperty.call(defaultsData, id)) return false;
    var defaultEntry = defaultsData[id];
    var payload = {
      name: defaultEntry.name,
      type: defaultEntry.type,
      description: defaultEntry.description,
      steps: deepCopy(defaultEntry.steps),
      steps_arms: deepCopy(defaultEntry.steps_arms)
    };
    return fetch('/movements/' + encodeURIComponent(id), {
      method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().then(function (d) { return {ok: r.ok, d: d}; });
    }).then(function (res) {
      if (!res.ok) {
        var strip = el('meErrorStrip');
        strip.hidden = false;
        strip.textContent = res.d.error;
        if (!strip.textContent) strip.textContent = 'Restore failed.';
        notify(res.d.error || 'Restore failed.', 'error');
        return false;
      }
      clearDraft();
      clearConfirmStrip();
      return fetchPool().then(function () {
        var restored = pool[id] || defaultEntry;
        el('meErrorStrip').hidden = true;
        el('meErrorStrip').textContent = '';
        isCreating = false;
        draft = {id: id, entry: deepCopy(restored), baseSnapshot: deepCopy(restored)};
        savedSnapshot = stableStringify(draft.entry);
        renderEditor();
        notify('Restored default', 'success');
        return true;
      });
    }).catch(function (error) {
      var strip = el('meErrorStrip');
      strip.hidden = false;
      strip.textContent = error && error.message ? error.message : 'Restore failed.';
      notify(strip.textContent, 'error');
      return false;
    });
  }
  function handleConfirmClick(event) {
    var button = event.target.closest('[data-action]');
    if (!button) return;
    var action = button.getAttribute('data-action');
    if (action === 'raw-apply-pending') {
      var applyNext = rawPendingAction;
      clearConfirmStrip();
      var applied = applyRaw(el('meRawText').value);
      if (applied.ok && applyNext === 'save') saveEntry();
      return;
    }
    if (action === 'discard-unsaved') {
      var proceed = pendingGuardAction;
      pendingGuardAction = null;
      clearConfirmStrip();
      discardCurrentEdits();
      if (proceed) proceed();
      return;
    }
    if (action === 'keep-editing') {
      pendingGuardAction = null;
      clearConfirmStrip();
      return;
    }
    if (action === 'confirm-restore-default' && pendingAction && pendingAction.type === 'restore-default') {
      var restoreId = pendingAction.id;
      pendingAction = null;
      clearConfirmStrip();
      restoreDefault(restoreId);
      return;
    }
    if (action === 'raw-discard-pending') {
      var discardNext = rawPendingAction;
      el('meRawText').value = rawSnapshot;
      rawPendingAction = null;
      clearConfirmStrip();
      if (discardNext === 'visual') setEditorMode('visual');
      else if (discardNext === 'save') saveEntry();
      return;
    }
    if (action === 'cancel' || action === 'cancel-delete') {
      pendingAction = null; pendingClearSnapshot = null; clearConfirmStrip(); return;
    }
    if (action === 'confirm-clear-branch' && pendingAction && pendingAction.type === 'clear-branch') {
      confirmClearBranch(); return;
    }
    if (action === 'confirm-import-branch' && pendingAction && pendingAction.type === 'import-branch') {
      var importAction = pendingAction;
      pendingAction = null;
      applyBranchImport(importAction);
      return;
    }
    if (action === 'confirm-remove-arms-variant' && pendingAction && pendingAction.type === 'remove-arms-variant') {
      pendingAction = null; removeArmsVariant(); return;
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
  function deleteMovement(id, discarded) {
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
    if (!discarded && draft && draft.id === id && (isDirty() || rawDirty())) {
      return meConfirmDiscard(function () { deleteMovement(id, true); });
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
      if (draft && draft.id === id) closeEditor(true);
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
  document.addEventListener('click', handleOverflowOutsideClick);
  document.addEventListener('visibilitychange', handleVisibilityChange);
  if (el('meLibSearch')) el('meLibSearch').addEventListener('input', renderLibrary);
  if (el('meLibSort')) el('meLibSort').addEventListener('change', function () {
    selectedSort = el('meLibSort').value || 'id'; renderLibrary();
  });
  if (el('movements-tab')) el('movements-tab').addEventListener('shown.bs.tab', fetchArmsStatus);
  if (el('meResetBtn')) el('meResetBtn').addEventListener('click', showResetConfirmation);
  if (el('mePruneBtn')) el('mePruneBtn').addEventListener('click', showPruneConfirmation);
  if (el('meSaveBtn')) el('meSaveBtn').addEventListener('click', saveEntry);
  if (el('mePlayBtn')) el('mePlayBtn').addEventListener('click', playActiveBranch);
  initializeLiveToggle();
  window.addEventListener('beforeunload', handleBeforeUnload);
  if (el('meEditor')) {
    el('meEditor').addEventListener('click', handleBranchClick);
    el('meEditor').addEventListener('input', handleMetadataInput);
    el('meEditor').addEventListener('change', handleMetadataInput);
    el('meEditor').addEventListener('click', handleModeClick);
    el('meEditor').addEventListener('click', handleOverflowClick);
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
  if (el('meImportReplace')) el('meImportReplace').addEventListener('click', function () { importIntoBranch('replace'); });
  if (el('meImportAppend')) el('meImportAppend').addEventListener('click', function () { importIntoBranch('append'); });
  if (el('meAddRef')) el('meAddRef').addEventListener('click', function () {
    var select = el('meRefSelect');
    if (select) addRef(select.value);
  });
  if (el('meClearBranch')) el('meClearBranch').addEventListener('click', function () { clearBranch(branch); });
  if (el('mePosePanel')) el('mePosePanel').addEventListener('click', handlePoseClick);
  fetchPool(); fetchDefaults(); fetchArmsStatus();




});