"""M0 Movement Studio contract pins (source-level; runtime audit uses a DOM stub)."""
import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = REPO_ROOT / "src" / "www" / "templates"
MOVEMENTS_HTML = (TEMPLATE_ROOT / "tabs" / "movements.html").read_text()
INDEX_HTML = (TEMPLATE_ROOT / "index.html").read_text()
MAIN_JS = (REPO_ROOT / "src" / "www" / "static" / "js" / "main.js").read_text()
JS_PATH = REPO_ROOT / "src" / "www" / "static" / "js" / "movement_editor.js"
JS = JS_PATH.read_text()


def top_level_functions():
    declarations = list(re.finditer(r"^  function ([A-Za-z_$][\w$]*)\([^\n]*\)\s*\{", JS, re.M))
    for index, match in enumerate(declarations):
        end = declarations[index + 1].start() if index + 1 < len(declarations) else JS.rfind("\n});")
        yield match.group(1), JS[match.start():end]


def function_source(name):
    """Return one named top-level module function, not a global needle bucket."""
    for function_name, source in top_level_functions():
        if function_name == name:
            return source
    raise AssertionError(f"function {name} is not defined as a top-level module function")


def test_movements_html_is_studio_shell_with_library_contract():
    for needle in (
        'id="meLibSearch"', 'id="meLibTypeFilter"', 'id="meLibSort"',
        'id="meLibCount"', 'id="meLibList"', 'id="meNewBtn"',
        'id="meResetBtn"', 'id="mePruneBtn"', 'id="meConfirmStrip"',
    ):
        assert needle in MOVEMENTS_HTML
    assert 'placeholder="Search movements…"' in MOVEMENTS_HTML


def test_no_duplicate_ids_in_scanned_templates():
    # Plan M0-S4 test 6 (name restored per audit M0-B F3): resolved include set
    # of index.html, ZERO exclusions (R2-F6) — no id may appear twice.
    include_re = re.compile(r"{%\s*include\s+'([^']+)'\s*%}")
    queue = [TEMPLATE_ROOT / "index.html"]
    seen = set()
    while queue:
        path = queue.pop().resolve()
        if path in seen:
            continue
        seen.add(path)
        queue.extend(TEMPLATE_ROOT / name for name in include_re.findall(path.read_text()))
    ids = [value for path in seen for value in re.findall(r'id="([^"]+)"', path.read_text())]
    assert len(ids) == len(set(ids)), sorted({value for value in ids if ids.count(value) > 1})


def test_legacy_json_editor_removed():
    legacy_ids = (
        "mvResetBtn", "mvPruneBtn", "mvError", "mvNewId", "mvNewBtn",
        "mvPoolList", "mvEditor", "mvEditorId", "mvName", "mvType",
        "mvDescription", "mvSteps", "mvStepsArms", "mvSaveBtn", "mvCancelBtn",
    )
    for element_id in legacy_ids:
        assert f'id="{element_id}"' not in MOVEMENTS_HTML
    assert 'class="me-legacy"' not in MOVEMENTS_HTML
    assert "function createMovement()" not in MOVEMENTS_HTML and "function saveEditor()" not in MOVEMENTS_HTML
    script_blocks = re.findall(r"<script>.*?</script>", MOVEMENTS_HTML, re.S)
    legacy_sha256 = "f088ea1efd365a89d96e3c2cb37b13c8aa33e52b5e3c710cfcaedb468be4c2e5"
    assert all(hashlib.sha256(script.encode()).hexdigest() != legacy_sha256 for script in script_blocks)


def test_index_loads_movement_editor_after_main_before_tars_preview():
    js_tag = re.search(r'<script[^>]+movement_editor\.js[^>]*></script>', INDEX_HTML)
    css_tag = re.search(r'<link[^>]+movement_editor\.css[^>]*>', INDEX_HTML)
    assert js_tag and len(re.findall(r'movement_editor\.js', INDEX_HTML)) == 1
    assert '?v=7.7' in js_tag.group(0)
    assert INDEX_HTML.index("js/main.js") < js_tag.start() < INDEX_HTML.index("js/tars_preview.js")
    assert css_tag and len(re.findall(r'movement_editor\.css', INDEX_HTML)) == 1
    assert '?v=7.7' in css_tag.group(0)
    assert INDEX_HTML.index("css/main.css") < css_tag.start()
    assert (REPO_ROOT / "src" / "www" / "static" / "css" / "movement_editor.css").stat().st_size > 100


def test_tab_ids_still_eight_with_movements_at_index_five():
    tabs = re.search(r"const TAB_IDS = \[([^\]]+)\]", MAIN_JS)
    btns = re.search(r"const TAB_BTN_IDS = \[([^\]]+)\]", MAIN_JS)
    assert tabs and btns
    tab_values = re.findall(r"'([^']+)'", tabs.group(1))
    btn_values = re.findall(r"'([^']+)'", btns.group(1))
    assert len(tab_values) == 8 and tab_values[5] == 'movements'
    assert len(btn_values) == 8 and btn_values[5] == 'movements-tab'


def test_index_includes_movements_html_exactly_once():
    assert len(re.findall(r"{%\s*include\s+'tabs/movements\.html'\s*%}", INDEX_HTML)) == 1


def test_boot_fetches_pool_with_error_and_retry_state():
    fetch_pool = function_source("fetchPool")
    render_error = function_source("renderError")
    click_handler = function_source("handleLibraryClick")
    assert "fetch('/movements'" in fetch_pool and "if (!r.ok)" in fetch_pool
    assert "Failed to load movements." in render_error
    assert "meLibRetry" in render_error and "data-action" in render_error
    assert "retry" in click_handler and "fetchPool()" in click_handler
    assert "el('movements').addEventListener('click', handleLibraryClick)" in JS
    assert "renderError()" in fetch_pool


def test_no_silent_fetches_in_movement_editor():
    assert not re.search(r"\.catch\s*\(\s*function\s*\(\s*\)\s*\{\s*\}\s*\)", JS)
    fetch_functions = [(name, source) for name, source in top_level_functions() if "fetch(" in source]
    assert fetch_functions
    for name, source in fetch_functions:
        assert re.search(r"\br\.ok\b", source), f"{name} must check HTTP status"


def test_library_row_contract_data_mid_and_summary():
    render = function_source("renderLibrary")
    action_button = function_source("makeActionButton")
    summary = function_source("entrySummary")
    for action in ("play", "edit", "dup", "del"):
        assert action in action_button
    assert "setAttribute('data-mid', id)" in render
    assert "textContent = id" in render
    assert "data-default-status" in render
    assert "entry.name" in render and "entry.description" in render
    assert "entry.steps_arms" in summary and "Math.min(10" in summary
    assert "return {steps: steps.length, hold: hold}" in summary
    assert "steps · " in render and "toFixed(1)" in render
    click_handler = function_source("handleLibraryClick")
    assert "else if (action === 'edit') openForEdit(id);" in click_handler
    assert "openLegacyEditor" not in JS and "handleLegacySave" not in JS


def test_library_search_filter_sort_count_wiring():
    matches = function_source("matchesFilters")
    sort_key = function_source("sortKey")
    compare = function_source("compareEntries")
    render = function_source("renderLibrary")
    click_handler = function_source("handleLibraryClick")
    assert "entry.name" in matches and "entry.description" in matches
    assert "var textMatches = !query || searchable.indexOf(query) !== -1;" in matches
    assert "var typeMatches = selectedType === 'all' || entry.type === selectedType;" in matches
    assert "return textMatches && typeMatches;" in matches
    assert "selectedSort === 'name'" in sort_key and "selectedSort === 'type'" in sort_key
    assert "return id.toLocaleLowerCase()" in sort_key and "localeCompare" in compare
    assert re.search(r"\|\|\s*a\.id\.localeCompare\(b\.id\)", compare)
    assert ".filter(function (item) { return matchesFilters(item.id, item.entry); })" in render
    assert ".sort(compareEntries)" in render
    assert "No movements match." in render and "clear-filters" in render
    assert "filtered.length + ' of ' + ids.length" in render
    assert "ids.length + ' movements · ' + legs + ' legs · ' + arms + ' arms'" in render
    assert "data-type" in click_handler and "meLibSearch" in JS and "input" in JS
    assert 'data-type="all"' in MOVEMENTS_HTML
    assert 'data-type="legs_only"' in MOVEMENTS_HTML
    assert 'data-type="has_arms"' in MOVEMENTS_HTML
    assert re.search(r'<option value="id"[^>]*selected', MOVEMENTS_HTML)
    for value in ("name", "type"):
        assert f'<option value="{value}"' in MOVEMENTS_HTML


def test_create_button_bridges_to_legacy_create_in_m0():
    click_handler = function_source("handleLibraryClick")
    assert "meNewBtn" in click_handler and "openCreate()" in click_handler
    assert "mvNewId" not in click_handler
    for bridge_symbol in ("openLegacyEditor", "handleLegacySave", "disarmLegacyBridge", "legacyEditingId"):
        assert bridge_symbol not in JS


def test_editor_shell_ids_present():
    for element_id in (
        "meEditor", "meBackBtn", "meIdInput", "meNameInput", "meTypeSelect",
        "meDescInput", "meSaveBtn", "meDirtyDot", "meErrorStrip", "meModeVisual",
        "meModeRaw", "meTimeline", "meAddStep", "mePosePanel", "meDraftBanner",
    ):
        assert f'id="{element_id}"' in MOVEMENTS_HTML


def test_create_auto_slug_and_id_lock():
    slug = function_source("slugify")
    render = function_source("renderEditor")
    metadata = function_source("handleMetadataInput")
    assert "toLowerCase()" in slug
    assert "replace(/\\s+/g, '_')" in slug and "replace(/[^a-z0-9_]/g, '')" in slug
    assert "idManuallyEdited" in metadata and "slugify" in metadata
    assert "id exists" in JS
    assert "meIdLock" in render
    assert "Ids are referenced by the D-pad, gamepad and voice assistant; renaming breaks them. Use Duplicate to fork." in render


def test_id_regex_literal_pinned():
    assert re.search(r"/\^\[a-z0-9_\]\+\$/", JS)


def test_dirty_uses_stable_stringify_snapshot():
    opened = function_source("openForEdit")
    dirty = function_source("isDirty")
    assert "savedSnapshot = stableStringify(draft.entry)" in opened
    assert "stableStringify(draft.entry) !== savedSnapshot" in dirty
    assert "function markDirty()" in JS and "dirty ? 'DIRTY' : 'CLEAN'" in function_source("markDirty")


def test_timeline_card_contract():
    render = function_source("renderTimeline")
    summary = function_source("stepSummary")
    assert "meStepCard" in render and "data-step-index" in render and "data-kind" in render
    assert "'pose'" in render and "'ref'" in render
    assert "selectStep(Number(card.getAttribute('data-step-index')))" in function_source("handleTimelineClick")
    for glyph in ("LH ", "RH ", "LL ", "RL ", "⏱ ", "⏸ ", "–", "H", "·"):
        assert glyph in summary
    ref_summary = summary.split("if (step && step.movement)", 1)[1].split("function valueFor", 1)[0]
    assert "⚡" in ref_summary and "⏸" not in ref_summary


def test_add_step_seed_contains_all_keys_plus_arm_speed():
    seed = function_source("DEFAULT_STEP")
    add = function_source("addStep")
    duplicate = function_source("dupStep")
    fields = (
        "left_height", "right_height", "left_leg", "right_leg", "left_main",
        "left_forearm", "left_hand", "right_main", "right_forearm", "right_hand",
        "speed", "hold_time", "movement", "arm_speed",
    )
    for field in fields:
        assert f"{field}:" in seed
    assert "arm_speed: null" in seed
    assert "DEFAULT_STEP()" in add and "steps.push" in add and "scrollIntoView" in add
    assert "deepCopy" in duplicate and "currentIndex + 1" in duplicate and "splice" in duplicate


def test_delete_step_undo_toast():
    delete = function_source("delStep")
    undo_toast = function_source("showUndoToast")
    assert "showUndoToast(function" in delete and "steps.splice(index, 0, removed)" in delete
    assert "window.showToast" in undo_toast and "5000" in undo_toast
    assert "addEventListener('click'" in undo_toast and "onUndo()" in undo_toast
    assert "meDelStep" in function_source("renderSelectedStep")
    assert "delete-step" in function_source("renderTimeline")


def test_step_reorder_controls():
    render = function_source("renderTimeline")
    click = function_source("handleTimelineClick")
    move = function_source("moveStep")
    assert "setAttribute('draggable', 'true')" in render
    assert "data-control', 'meStepUp'" in render and "data-control', 'meStepDown'" in render
    assert "dragstart" in JS and "dragover" in JS and "drop" in JS
    assert "moveStep(index, index - 1)" in click and "moveStep(index, index + 1)" in click
    assert "steps.splice(from, 1)" in move and "steps.splice(to, 0, moved)" in move


def test_clear_branch_confirm_and_undo_scoped_to_one_branch():
    html = (TEMPLATE_ROOT / "tabs" / "movements.html").read_text()
    clear = function_source("clearBranch")
    confirmed = function_source("confirmClearBranch")
    assert 'id="meClearBranch"' in html and "meClearBranch" in JS
    assert "one neutral step" in clear and "confirm-clear-branch" in clear
    assert "draft.entry.steps = [DEFAULT_STEP()]" in confirmed
    assert "showUndoToast(function" in confirmed and "draft.entry.steps = before" in confirmed
    assert "steps_arms =" not in confirmed


def test_no_falsy_coercion_in_load_paths():
    assert "|| 50" not in JS and "|| 0.85" not in JS
    assert JS.count("=== null") >= 4


def test_mode_chips_derive_from_stored_value():
    derive = function_source("deriveMode")
    render = function_source("renderSelectedStep")
    handler = function_source("handlePoseClick")
    assert "value === null" in derive and "value === undefined" in derive and "value === 0" in derive
    assert "value === -1" in derive and "return 'hold'" in derive and "return 'set'" in derive
    assert "return 'skip'" in derive
    assert "Timing belongs to the referenced movement" in render
    assert "data-mode" in render and "deriveMode" in render
    assert "setStepField(field, null)" in handler


def test_two_way_binding_and_clamp_notes():
    render = function_source("renderSelectedStep")
    bind = function_source("bindServoRow")
    assert "meSlider" in render and "meNumber" in render
    assert "slider.min = '1'" in bind and "slider.max = '100'" in bind
    assert "0 means SKIP — use the SKIP mode" in bind and "Number(" in bind
    assert "parseInt" not in bind and "me-clamp" in bind
    assert "number.classList.add('me-clamp')" in bind and "note.textContent = noteText" in bind
    assert "slider.addEventListener('input'" in bind and "number.addEventListener('input'" in bind
    assert "number.addEventListener('change'" in bind and "number.addEventListener('blur'" in bind


def test_timing_footer_ranges_visual_cap_at_ten():
    render = function_source("renderSelectedStep")
    timing = function_source("renderTimingField")
    assert "renderTimingField" in render
    assert "slider.id = config.sliderId" in timing and "number.id = config.numberId" in timing
    assert "meSpeedSlider" in JS and "meSpeedNumber" in JS and "meHoldSlider" in JS and "meHoldNumber" in JS
    timing_fields = re.search(r"var TIMING_FIELDS = \[(.*?)\n  \];", JS, re.S)
    assert timing_fields
    assert "step: 0.05" in timing_fields.group(1)
    assert re.search(r"field: 'hold_time'.*?max: 10", timing_fields.group(1))
    assert "ramp speed · higher = faster" in timing_fields.group(1)
    assert "slider.max = String(config.max)" in timing


def test_validation_mirrors_server_rules():
    validate = function_source("validateEntry")
    assert "^[a-z0-9_]+$" in JS and ".trim()" in validate
    assert "normalized.name.trim()" in validate and "normalized.description.trim()" in validate
    assert "legs_only" in validate and "has_arms" in validate
    assert "steps must be a non-empty list" in validate and "must be a list or null" in validate
    assert "0..100" in validate and "0..1" in validate
    assert "step.hold_time > 10" in validate and "clamped to 10 on save" in validate


def test_missing_keys_auto_filled_and_unknown_keys_rejected():
    validate = function_source("validateEntry")
    for field in (
        "left_height", "right_height", "left_leg", "right_leg", "left_main", "left_forearm",
        "left_hand", "right_main", "right_forearm", "right_hand", "speed", "hold_time", "movement", "arm_speed",
    ):
        assert f"{field}:" in JS
    assert "STEP_DEFAULTS_MIRROR" in JS and "Filled " in validate
    assert "step[key] = deepCopy(STEP_DEFAULTS_MIRROR[key])" in validate and "filledCount++" in validate
    assert "if (_STEP_KEYS.indexOf(key) === -1)" in validate and "unknown field" in validate
    assert "['name', 'type', 'description', 'steps', 'steps_arms']" in validate


def test_save_state_machine_and_movements_family_exclusive():
    save = function_source("saveEntry")
    for state in ("CLEAN", "DIRTY", "SAVING", "SAVED", "ERROR"):
        assert state in JS
    assert "fetch('/movements'," in save and "method: 'POST'" in save
    assert "fetch('/movements/'" in save and "method: 'PUT'" in save
    assert "steps_arms:" in save and "r.ok" in save
    assert "renderEditor(true)" in save and save.index("renderEditor(true)") < save.index("setSaveState('SAVED')")
    assert "'/save_sequence'" not in JS


def test_create_flip_locks_id_and_surfaces_server_errors():
    save = function_source("saveEntry")
    assert "isCreating = false" in save and "renderEditor(true)" in save and "meIdLock" in function_source("renderEditor")
    assert "strip.textContent = res.d.error" in save
    assert "meNameInput').value = ''" not in save


def test_raw_never_reads_or_writes_id():
    render = function_source("renderRaw")
    serializer = function_source("rawEntryFromDraft")
    validate = function_source("validateEntry")
    assert "['name', 'type', 'description', 'steps', 'steps_arms']" in serializer
    assert "id:" not in render and "id is supplied by the editor" in validate
    assert "unknown top-level key" in validate or "key === 'id'" in validate


def test_raw_apply_pipeline_and_intercepts():
    html = (TEMPLATE_ROOT / "tabs" / "movements.html").read_text()
    apply = function_source("applyRaw")
    toggle = function_source("handleModeClick")
    save = function_source("saveEntry")
    assert 'id="meRawText"' in html and 'id="meRawApply"' in html
    assert "JSON.parse" in apply and "validateEntry" in apply and "slice(0, 5)" in apply
    assert "validation.notice" in apply and "Apply or discard JSON changes first." in JS
    assert "rawDirty()" in toggle and "rawDirty()" in save


def test_draft_key_debounce_clear():
    write = function_source("writeDraft")
    schedule = function_source("scheduleDraftWrite")
    save = function_source("saveEntry")
    banner = function_source("showDraftBanner")
    assert "tars.movementEditor.draft.v1" in JS and "500" in schedule
    assert "window.localStorage.setItem" in write and "removeItem" in function_source("clearDraft")
    assert "clearDraft()" in save and "meDraftBanner" in banner
    assert "[Restore]" in banner and "[Discard]" in banner


def test_draft_drift_and_quota_degrade():
    write = function_source("writeDraft")
    banner = function_source("showDraftBanner")
    check = function_source("checkDraftBanner")
    assert "changed on the robot since" in banner
    assert "QuotaExceededError" in write and "quotaNoticeShown" in write
    assert "baseSnapshot" in banner and "stableStringify" in banner
    assert "restoreDraft(" not in check


def test_dirty_guards_on_in_app_navigation():
    assert "Discard unsaved changes to" in JS
    for name in ("openForEdit", "openCreate", "closeEditor", "duplicateMovement", "deleteMovement"):
        assert "meConfirmDiscard(" in function_source(name), f"{name} must guard dirty navigation"
    delete = function_source("deleteMovement")
    assert delete.index("findReferrers(id, pool)") < delete.index("meConfirmDiscard(")
    assert "'beforeunload'" in JS


def test_editor_overflow_menu_contract():
    html = (TEMPLATE_ROOT / "tabs" / "movements.html").read_text()
    render = function_source("renderOverflowMenu")
    gate = function_source("shouldShowRestoreDefault")
    assert 'id="meOverflowBtn"' in html and 'id="meOverflowMenu"' in html
    for action in ("duplicate", "delete", "restore-default", "close"):
        assert f"'{action}'" in render
    assert "defaultsOk" in gate and "defaultsData" in gate
    assert "stableStringify(draft.entry) !== stableStringify(defaultsData[draft.id])" in gate
    assert "shouldShowRestoreDefault()" in render
    assert "!isCreating" in render
    click = function_source("handleOverflowClick")
    assert "renderOverflowMenu()" in click


def test_restore_default_puts_default_entry_with_confirm():
    restore = function_source("restoreDefault")
    confirm = function_source("showRestoreDefaultConfirm")
    assert "var defaultEntry = defaultsData[id]" in restore
    assert "name: defaultEntry.name" in restore and "steps_arms: deepCopy(defaultEntry.steps_arms)" in restore
    assert "method: 'PUT'" in restore and "fetch('/movements/'" in restore
    assert "Restore '" in confirm and "to the shipped default" in confirm and "Your edits are lost." in confirm
    assert "strip.textContent = res.d.error" in restore


def test_badges_use_defaults_with_degraded_mode():
    defaults = function_source("fetchDefaults")
    badges = function_source("applyBadges")
    stable = function_source("stableStringify")
    assert "fetch('/movements/defaults'" in defaults and "if (!r.ok)" in defaults
    assert "defaultsOk = false" in defaults and "notify(" in defaults and "catch" in defaults
    assert "default" in badges and "modified" in badges and "custom" in badges
    assert "data-default-status" in badges and "style.display = 'none'" in badges
    assert "stableStringify(pool[id])" in badges and "stableStringify(defaultsData[id])" in badges
    assert "Array.isArray" in stable and ".sort()" in stable
    assert "var status = !inDefaults ? 'custom' :" in badges
    assert "stableStringify(pool[id]) === stableStringify(defaultsData[id]) ? 'default' : 'modified'" in badges


def test_play_calls_play_saved_sequence_and_surfaces_errors():
    play = function_source("playMovement")
    assert "fetch('/play_saved_sequence'" in play and "method: 'POST'" in play
    assert "JSON.stringify({name: id})" in play and "if (!r.ok)" in play
    assert "res.d.error" in play and "notify(res.d.error" in play
    assert "button.disabled = true" in play and "button.disabled = false" in play


def test_duplicate_posts_full_copy_with_free_suffix():
    duplicate = function_source("duplicateMovement")
    assert "'_copy'" in duplicate and "_copy' + suffix" in duplicate
    assert "var newId = id + '_copy';" in duplicate
    assert "while (Object.prototype.hasOwnProperty.call(pool, newId))" in duplicate
    assert "steps: deepCopy(entry.steps)" in duplicate
    assert "steps_arms: deepCopy(entry.steps_arms)" in duplicate
    assert "method: 'POST'" in duplicate and "fetch('/movements'" in duplicate
    assert "if (!r.ok)" in duplicate and "res.d.error" in duplicate
    assert "fetchPool()" in duplicate


def test_delete_computes_pool_referrers_before_any_fetch():
    find = function_source("findReferrers")
    delete = function_source("deleteMovement")
    perform = function_source("performDelete")
    assert "Object.keys(sourcePool)" in find and "['steps', 'steps_arms']" in find
    assert "step.movement === targetId" in find
    confirm = function_source("handleConfirmClick")
    assert "findReferrers(id, pool)" in delete and "fetch(" not in delete
    assert "confirm-delete" in confirm and "performDelete(id)" in confirm
    assert "addEventListener('click', handleConfirmClick)" in JS
    assert "fetch('/movements/'" in perform and "method: 'DELETE'" in perform


def test_delete_shipped_warning_and_no_window_confirm():
    delete = function_source("deleteMovement")
    assert 'is referenced by' in delete and 'ALL custom movements are silently replaced by defaults' in delete
    assert 'is a shipped movement' in delete and 'D-pad, gamepad and voice assistant' in delete
    assert 'Delete anyway' in delete and 'confirm-delete' in delete
    assert "window.confirm" not in JS
    guard = delete.split("if (refs.length)", 1)[1].split("pendingAction =", 1)[0]
    assert "action: 'cancel-delete'" in guard
    assert "confirm-delete" not in guard and "performDelete" not in guard and "fetch(" not in guard


def test_reset_prune_inline_strip_and_error_surfacing():
    reset = function_source("showResetConfirmation")
    perform = function_source("performReset")
    click = function_source("handleConfirmClick")
    assert "Restore every default movement?" in reset
    assert "Edits to shipped movements are lost; custom movements are kept." in reset
    assert "fetch('/movements/reset'" in perform and "method: 'POST'" in perform
    assert "prune: prune" in perform and "if (!r.ok)" in perform
    assert "res.d.error" in perform and "showConfirmStrip(res.d.error" in perform
    assert "confirm-reset" in click and "performReset(false)" in click
    assert "fetch(" not in reset
    assert "addEventListener('click', showResetConfirmation)" in JS
    assert "addEventListener('click', showPruneConfirmation)" in JS
    assert "addEventListener('click', handleConfirmClick)" in JS
    assert "window.confirm" not in JS


def test_prune_preview_lists_custom_ids_and_defensive_ref_check():
    preview = function_source("showPruneConfirmation")
    find = function_source("findReferrers")
    click = function_source("handleConfirmClick")
    assert "Object.keys(pool).filter" in preview
    assert "hasOwnProperty.call(defaultsData || {}, id)" in preview
    assert "custom movements" in preview and "custom.join(', ')" in preview
    assert "findReferrers(id, defaultsData)" in preview and "referrers.length" in preview
    assert "Prune anyway" in preview and "confirm-prune" in click and "prune-anyway" in click
    assert "step.movement === targetId" in find
    assert "fetch(" not in preview


