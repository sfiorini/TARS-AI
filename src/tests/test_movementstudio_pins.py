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
    assert '?v=8.0' in js_tag.group(0)
    assert "filename='js/main.js') }}?v=8.0" in INDEX_HTML
    assert INDEX_HTML.index("js/main.js") < js_tag.start() < INDEX_HTML.index("js/tars_preview.js")
    assert css_tag and len(re.findall(r'movement_editor\.css', INDEX_HTML)) == 1
    assert '?v=7.9' in css_tag.group(0)
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


def test_branch_bar_and_per_branch_state():
    html = (TEMPLATE_ROOT / "tabs" / "movements.html").read_text()
    for branch_id, branch_value, subtitle in (
        ("meBranchBase", "steps", "plays when arms are absent"),
        ("meBranchArms", "steps_arms", "plays when arms are installed"),
    ):
        assert f'id="{branch_id}"' in html
        assert f'data-branch="{branch_value}"' in html
        assert subtitle in html
    assert re.search(
        r"var branchState = \{\s*steps: \{index: 0, scrollTop: 0, undoStack: \[\]\},\s*"
        r"steps_arms: \{index: 0, scrollTop: 0, undoStack: \[\]\}\s*\};", JS
    )
    branch_steps = function_source("branchSteps")
    assert "draft.entry[name] || []" in branch_steps
    switch = function_source("setBranch")
    assert "branchState[branch].scrollTop = timeline.scrollTop" in switch
    assert "selected = branchState[name]" in switch
    assert "timeline.scrollTop = branchState[name].scrollTop" in switch
    assert "renderTimeline()" in switch
    timeline = function_source("renderTimeline")
    pose = function_source("renderSelectedStep")
    selected_step = function_source("selectedStep")
    assert "var steps = branchSteps(branch)" in timeline
    assert "var steps = branchSteps(branch)" in selected_step
    assert "var step = selectedStep()" in pose
    clear = function_source("clearBranch")
    confirm_clear = function_source("confirmClearBranch")
    assert "var branch = branchName || activeBranch()" in clear
    assert "branchState[branchName].undoStack.push(undoRecord)" in confirm_clear


def test_plays_now_status_rules():
    html = (TEMPLATE_ROOT / "tabs" / "movements.html").read_text()
    assert 'id="meBranchStatus"' in html
    plays_now = function_source("playsNow")
    for copy in (
        "On this robot: ARMS variant plays (",
        "On this robot: BASE plays — no arms variant",
        "Arms not installed: BASE (leg portion) plays",
        "On this robot: BASE plays",
    ):
        assert copy in plays_now
    assert "if (armsPresent && armsSteps.length)" in plays_now
    assert "draft.entry.type === 'has_arms'" in plays_now
    assert "return 'On this robot: BASE plays'" in plays_now
    render_status = function_source("renderBranchStatus")
    assert "status.textContent = !draft ? ''" in render_status and ": playsNow()" in render_status
    fetch_arms = function_source("fetchArmsStatus")
    assert "fetch('/get_arms_status')" in fetch_arms and "if (!r.ok)" in fetch_arms
    assert "armsPresent = !!d.arms_present" in fetch_arms and "renderBranchStatus()" in fetch_arms
    assert "renderBranchStatus()" in function_source("setBranch")
    assert "renderBranchStatus()" in function_source("renderEditor")
    assert "shown.bs.tab" in JS and "fetchArmsStatus()" in JS


def test_arms_variant_creation_and_removal_flows():
    html = (TEMPLATE_ROOT / "tabs" / "movements.html").read_text()
    timeline = function_source("renderTimeline")
    create = function_source("createArmsFromBase")
    start = function_source("startArmsEmpty")
    remove = function_source("showRemoveArmsVariantConfirm")
    render_menu = function_source("renderOverflowMenu")
    overflow_click = function_source("handleOverflowClick")
    confirm = function_source("handleConfirmClick")
    assert "steps_arms === null" in timeline or "!Array.isArray(draft.entry.steps_arms)" in timeline
    assert "Create from BASE" in timeline and "Start empty" in timeline
    assert "draft.entry.steps_arms = deepCopy(draft.entry.steps)" in create
    assert "draft.entry.steps_arms = [DEFAULT_STEP()]" in start
    assert "Remove arms variant" in render_menu and "steps_arms.length" in render_menu
    assert "Delete the ' + draft.entry.steps_arms.length + '-step arms variant? BASE is kept.'" in remove
    assert "action === 'remove-arms-variant'" in overflow_click and "showRemoveArmsVariantConfirm()" in overflow_click
    assert "confirm-remove-arms-variant" in remove and "removeArmsVariant()" in confirm
    assert "function removeArmsVariant()" in JS and "draft.entry.steps_arms = null" in function_source("removeArmsVariant")
    assert "data-action=\"create-arms-from-base\"" in timeline or "create-arms-from-base" in timeline
    assert 'id="meOverflowMenu"' in html


def test_empty_arms_variant_normalizes_to_null_on_visual_save():
    save = function_source("saveEntry")
    assert "var rawSave = editorMode === 'raw'" in save
    assert "!rawSave && Array.isArray(draft.entry.steps_arms) && draft.entry.steps_arms.length === 0" in save
    assert "draft.entry.steps_arms = null" in save
    assert "notify('Empty arms variant removed'" in save


def test_arms_group_gated_by_arms_present():
    render = function_source("renderSelectedStep")
    fields = (
        "left_main", "left_forearm", "left_hand",
        "right_main", "right_forearm", "right_hand",
    )
    for field in fields:
        assert f"field: '{field}'" in JS
    assert "if (armsPresent)" in render
    assert "SERVO_FIELDS.filter(function (config) { return config.group === 'arms'; })" in render
    assert "row.setAttribute('data-field', config.field)" in render
    assert "Arm servos not installed — arm fields stay editable in RAW mode and are stored, but not played." in render
    assert "Arm values play on PLAY — live preview is legs-only in this version." in render
    assert "steps_arms: deepCopy(draft.entry.steps_arms)" in function_source("saveEntry")
    assert "steps_arms" in function_source("rawEntryFromDraft")
    # M2 audit-B F2 (E5 data-loss): the save path must NEVER consult the
    # hardware flag — arms are stored even when not installed.
    assert "armsPresent" not in function_source("saveEntry")


def test_hold_mode_stores_minus_one():
    render = function_source("renderSelectedStep")
    handle = function_source("handlePoseClick")
    set_field = function_source("setStepField")
    derive = function_source("deriveMode")
    assert "config.group === 'arms' ? ['skip', 'hold', 'set']" in render
    assert "chip.setAttribute('data-mode', mode)" in render
    assert "chip.title = 'Re-poke the last position (keeps this arm energized).'" in render
    assert "display.textContent = storedMode === 'hold' ? 'HOLD'" in render
    assert "number.placeholder = storedMode === 'hold' ? 'HOLD'" in render
    assert "if (mode === 'hold') setStepField(field, -1)" in handle
    assert "value === -1" in set_field and "step[field] = -1" in set_field
    assert "value === -1" in derive and "return 'hold'" in derive


def test_arm_speed_ui_and_null_auto():
    render = function_source("renderSelectedStep")
    arm_speed = function_source("renderArmSpeedField")
    bind = function_source("bindArmSpeedControls")
    assert "renderArmSpeedField(step, timingFooter)" in render
    assert "id = 'meArmSpeedAuto'" in arm_speed and "Auto (= speed)" in arm_speed
    assert "auto.addEventListener('change'" in bind and "step.arm_speed = null" in bind
    assert "step.arm_speed = snapped" in bind and "slider.addEventListener('input', function () { setValue(Number(slider.value)); })" in bind
    assert "id = 'meArmSpeedSlider'" in arm_speed and "id = 'meArmSpeedNumber'" in arm_speed
    assert "slider.min = '0'" in arm_speed and "slider.max = '1'" in arm_speed and "slider.step = '0.05'" in arm_speed
    assert "number.min = '0'" in arm_speed and "number.max = '1'" in arm_speed
    assert "branch === 'steps_arms' || hasArmValue" in arm_speed
    # M2 audit-B F4: collapsed presentation is Auto (checked) when custom is
    # hidden — a stored arm_speed on a BASE step without arm values stays
    # visible-as-Auto and preserved, never an unchecked disabled limbo.
    assert "auto.checked = step.arm_speed === null || step.arm_speed === undefined || !showCustom;" in arm_speed


def test_import_from_cached_pool_per_branch():
    html = (TEMPLATE_ROOT / "tabs" / "movements.html").read_text()
    controls = function_source("renderComposerControls")
    import_branch = function_source("importIntoBranch")
    apply_import = function_source("applyBranchImport")
    confirm = function_source("handleConfirmClick")
    for element_id in ("meImportSelect", "meImportReplace", "meImportAppend"):
        assert f'id="{element_id}"' in html
    assert "filter(function (id) { return !draft || id !== draft.id; })" in controls
    assert "Import into ' + (branch === 'steps' ? 'BASE' : 'ARMS')" in controls
    assert "sourceEntry[branch]" in import_branch
    assert "kind === 'replace'" in import_branch and "Replace ' + target.length" in import_branch
    assert "branchLabel + ' steps with ' + sourceId" in import_branch
    assert "confirm-import-branch" in import_branch and "var importAction = pendingAction" in confirm
    assert "applyBranchImport(importAction)" in confirm
    append_path = apply_import.split("else if (action.kind === 'append')", 1)[1]
    assert "kind === 'append'" in apply_import and "target.concat(imported)" in append_path
    assert "selected.index" not in append_path
    # M2 audit-A F1: pin each deepcopy by its EXACT line in the pre-branch
    # window — a function-global needle is satisfied by the other path's copy
    # (append-deletion survived). Append consumes `imported` (copied up top);
    # replace copies inline. Both lines must stay deep copies.
    pre_branch = apply_import.split("else if (action.kind === 'append')", 1)[0]
    assert "var imported = deepCopy(sourceSteps);" in pre_branch
    assert "draft.entry[action.branch] = deepCopy(sourceSteps);" in pre_branch
    # M2 audit-B F3: substring ban covers query-string/concatenation variants.
    assert not re.search(r"get_movement_steps", JS)


def test_ref_cards_expose_no_timing_controls():
    render = function_source("renderSelectedStep")
    ref_panel = function_source("renderRefPanel")
    step_summary = function_source("stepSummary")
    assert "renderRefPanel(step, panel," in render
    assert "refNote.textContent = caption" in ref_panel
    assert "renderRefPanel(step, panel, 'Timing belongs to the referenced movement — this step\\'s stored hold/speed are not applied at playback.')" in render
    for timing_id in ("meHoldSlider", "meSpeedSlider", "meArmSpeedSlider"):
        assert timing_id not in ref_panel
    # M2 audit-B F1: the ref RENDER PATH (everything before the non-ref guard)
    # must not emit timing controls — pins the hoist mutation, not just the
    # structurally-timing-free renderRefPanel helper.
    ref_path = render.split("if (!step.movement) {", 1)[0]
    assert "TIMING_FIELDS.forEach" not in ref_path and "renderArmSpeedField" not in ref_path
    assert "plays " in step_summary and "playableSteps(reference)" in step_summary
    validate = function_source("validateEntry")
    assert "stored, not applied at playback" in validate


def test_ref_step_neutral_keys_and_open_link():
    html = (TEMPLATE_ROOT / "tabs" / "movements.html").read_text()
    controls = function_source("renderComposerControls")
    add_ref = function_source("addRef")
    ref_summary = function_source("refSummary")
    ref_panel = function_source("renderRefPanel")
    pose_click = function_source("handlePoseClick")
    for element_id in ("meRefSelect", "meAddRef"):
        assert f'id="{element_id}"' in html
    ref_options = controls.split("var refSelect = el('meRefSelect');", 1)[1]
    assert "filter(function (id) { return !draft || id !== draft.id; })" in ref_options
    for key in (
        "left_height", "right_height", "left_leg", "right_leg", "left_main", "left_forearm",
        "left_hand", "right_main", "right_forearm", "right_hand", "speed", "hold_time", "movement", "arm_speed",
    ):
        assert f"{key}:" in add_ref
    assert "speed: 0.85" in add_ref and "hold_time: 0" in add_ref and "arm_speed: null" in add_ref
    assert "movement: id" in add_ref and "playableSteps(entry)" in ref_summary
    assert "open ↗" in ref_panel and "data-ref-id" in ref_panel
    assert "openForEdit(open.getAttribute('data-ref-id'))" in pose_click


def test_ref_validation_client_dfs():
    validate = function_source("validateEntry")
    find_cycle = function_source("findCycle")
    assert "function findCycle(startId, entries)" in find_cycle
    assert "visited" in find_cycle and "active" in find_cycle
    assert "['steps', 'steps_arms']" in find_cycle
    assert "findCycle(id, cycleEntries)" in validate
    assert "step.movement === id" in validate
    assert "path: branchName + '[' + index + '].movement'" in validate


def test_promotion_demotion_flows_keep_variant():
    metadata = function_source("handleMetadataInput")
    notice = function_source("renderTypeNotice")
    click = function_source("handleBranchClick")
    save = function_source("saveEntry")
    assert "Add an arms variant?" in notice and "Create from base" in notice
    assert "Arms variant kept but unused while type is legs_only." in notice
    assert "create-arms-from-type" in notice and "createArmsFromBase()" in click
    assert "previousType === 'legs_only' && target.value === 'has_arms'" in metadata
    promotion = metadata.split("previousType === 'legs_only'", 1)[1].split("else if (target.id === 'meDescInput')", 1)[0]
    assert "createArmsFromBase()" not in promotion
    demotion = metadata.split("else if (target.id === 'meTypeSelect')", 1)[1].split("else return;", 1)[0]
    assert "previousType === 'has_arms' && target.value === 'legs_only'" in demotion
    assert not re.search(r"steps_arms\s*=", demotion)
    assert "type: draft.entry.type" in save
    assert "steps_arms: deepCopy(draft.entry.steps_arms)" in save


def test_play_sequence_branch_labeled_preflight_and_409():
    play = function_source("playActiveBranch")
    render_label = function_source("renderPlayButton")
    assert "el('mePlayBtn').addEventListener('click', playActiveBranch)" in JS
    assert "var playBranch = activeBranch();" in play
    assert "var playSteps = deepCopy(branchSteps(playBranch));" in play
    assert "steps: deepCopy(playSteps)," in play
    assert "var validation = validateEntry(validationEntry" in play
    assert "fetch('/play_sequence'" in play
    assert play.index("validateEntry(validationEntry") < play.index("fetch('/play_sequence'")
    assert "if (!validation.ok)" in play and "me-card-error" in play
    assert "selected.index = Number(badStep[1]);" in play
    assert "badCard.classList.add('me-card-error')" in play
    assert "displayPath + ': ' + firstError.msg" in play
    # M3 audit-B verification R-1: pin the SOURCE field of displayPath — the
    # F7 fix otherwise left firstError.path unpinned (wrong-field mutation survived).
    assert "var displayPath = firstError ? firstError.path : '';" in play
    # M3 audit-B F7: ARMS-branch errors display steps_arms[N] paths.
    assert r"if (playBranch === 'steps_arms') displayPath = displayPath.replace(/^steps\[/, 'steps_arms[');" in play
    # M3 audit-B F2: honesty copy — no mid-move stop in v1.
    assert 'title="Plays to completion — no mid-move stop in v1"' in MOVEMENTS_HTML
    assert "JSON.stringify({steps: playSteps})" in play
    assert "playInFlight = true;" in play and "playInFlight = false;" in play
    assert "refreshBusyUI();" in render_label and "playButton.disabled = robotMoving || playInFlight;" in function_source("refreshBusyUI")
    assert "res.status === 409" in play and "Robot is already moving" in play
    assert "startBusyPoll()" in play
    assert "branch === 'steps' ? 'PLAY BASE' : 'PLAY ARMS'" in render_label
    assert "renderPlayButton();" in function_source("renderEditor")
    assert "renderPlayButton();" in function_source("setBranch")
    movement_css = (REPO_ROOT / "src" / "www" / "static" / "css" / "movement_editor.css").read_text()
    assert ".meStepCard.me-card-error" in movement_css and "var(--red)" in movement_css
    # M3 audit-B F1: error cards are auto-selected; same specificity means the
    # error rule must be declared AFTER the selected rule to win the cascade.
    assert movement_css.index(".meStepCard.me-card-error") > movement_css.index(".meStepCard.me-selected")


def test_busy_poll_single_owner_and_lifecycle():
    schedule = function_source("scheduleBusyPoll")
    start = function_source("startBusyPoll")
    stop = function_source("stopBusyPoll")
    poll = function_source("pollBusy")
    visibility = function_source("handleVisibilityChange")
    play = function_source("playActiveBranch")
    assert "var busyTimer = null;" in JS
    assert "if (busyTimer !== null) return;" in start
    assert "setTimeout(pollBusy, 1500)" in schedule
    assert "if (document.hidden || (!playInFlight && !robotMoving && !playRecently)) return;" in schedule
    assert "fetch('/servo_status')" in poll and "if (!r.ok)" in poll
    assert "if (document.hidden) return false;" in poll
    assert "clearTimeout(busyTimer)" in stop and "busyTimer = null" in stop
    assert "!playInFlight && !robotMoving && !playRecently" in schedule
    assert "stopBusyPoll();" in visibility and "document.hidden" in visibility
    assert "document.addEventListener('visibilitychange', handleVisibilityChange)" in JS
    assert "startBusyPoll();" in play


def test_busy_chip_and_play_disable():
    busy_ui = function_source("refreshBusyUI")
    poll = function_source("pollBusy")
    render_library = function_source("renderLibrary")
    assert 'id="meBusyChip"' in MOVEMENTS_HTML
    assert "busyChip.hidden = !robotMoving;" in busy_ui
    assert "busyChip.textContent = robotMoving ? 'ROBOT BUSY' : '';" in busy_ui
    assert "playButton.disabled = robotMoving || playInFlight;" in busy_ui
    assert "button.disabled = robotMoving || playInFlight;" in busy_ui
    assert "Servo controller offline" in MOVEMENTS_HTML
    assert "offlineChip.hidden = pcaAlive !== false;" in busy_ui
    assert "refreshBusyUI();" in poll and "refreshBusyUI();" in render_library


def test_live_clamp_constants_and_skip_proxy():
    clamp = function_source("clampForLive")
    send = function_source("sendLive")
    servo_binding = function_source("bindServoRow")
    pose_click = function_source("handlePoseClick")
    assert "var LIVE_RANGES = {min: 5, max: 100, speedMin: 0.65, speedMax: 1.0};" in JS
    assert "var lastLiveLegValues = {left_height: 50, right_height: 50, left_leg: 50, right_leg: 50};" in JS
    assert "LIVE_RANGES.min" in clamp and "LIVE_RANGES.max" in clamp
    assert "LIVE_RANGES.speedMin" in clamp and "LIVE_RANGES.speedMax" in clamp
    assert "lastLiveLegValues[field]" in send
    assert "var value = step[field];" in send
    assert "value === null || value === undefined || value === 0 ? lastLiveLegValues[field]" in send
    assert "var liveValues = clampForLive(legs, step.speed);" in send
    assert "fetch('/move_legs'" in send
    assert send.index("clampForLive(legs, step.speed)") < send.index("fetch('/move_legs'")
    assert "body: JSON.stringify({" in send
    assert "left_height: liveValues.legs.left_height" in send
    assert "right_height: liveValues.legs.right_height" in send
    assert "left_leg: liveValues.legs.left_leg" in send
    assert "right_leg: liveValues.legs.right_leg" in send
    assert "speed: liveValues.speed" in send
    assert "if (liveValues.clamped) showLiveClampNotice();" in send
    assert "step[field] =" not in send and "step.speed =" not in send and "legs[field] =" not in clamp
    assert "slider.addEventListener('change'" in servo_binding
    assert "number.addEventListener('change', commitNumber)" in servo_binding
    assert "config.group === 'legs'" in servo_binding and "sendLive(step);" in servo_binding
    assert "sendLive(liveStep);" in pose_click
    assert "Live preview clamps to 5–100 · speed 0.65–1.0 (stored values unchanged)" in MOVEMENTS_HTML
    assert "SKIP servos hold their last live value in preview." in MOVEMENTS_HTML
    assert "clampNotice.hidden = false;" in function_source("showLiveClampNotice")
    assert "if (!liveClampToastShown)" in function_source("showLiveClampNotice")


def test_live_checks_r_ok_rate_limits_and_forces_off():
    send = function_source("sendLive")
    notify_error = function_source("notifyLiveError")
    force_off = function_source("forceLiveOff")
    close = function_source("closeEditor")
    visibility = function_source("handleVisibilityChange")
    init_toggle = function_source("initializeLiveToggle")
    assert 'id="meLiveToggle"' in MOVEMENTS_HTML and 'type="checkbox"' in MOVEMENTS_HTML
    assert "liveToggle.checked = false;" in init_toggle and "initializeLiveToggle();" in JS
    assert "if (!liveToggle || !liveToggle.checked) return false;" in send
    assert "fetch('/move_legs'" in send and "if (!r.ok)" in send
    assert "notifyLiveError" in send and "5000" in notify_error
    calls = re.findall(r"(?:window\.)?localStorage\.(?:getItem|setItem|removeItem)\(([^)]*)\)", JS)
    assert calls and all(argument.split(",", 1)[0].strip() == "DRAFT_KEY" for argument in calls)
    assert "var DRAFT_KEY = 'tars.movementEditor.draft.v1';" in JS
    assert "liveToggle" not in function_source("readDraft") + function_source("writeDraft")
    assert "toggle.checked = false;" in force_off
    assert "forceLiveOff();" in close and "forceLiveOff();" in visibility
    assert "if (document.hidden)" in visibility


def test_neutral_uses_move_legs_not_neutral_legs():
    neutral = function_source("sendNeutral")
    assert 'id="meNeutralBtn"' in MOVEMENTS_HTML
    assert "el('meNeutralBtn').addEventListener('click', sendNeutral)" in JS
    assert "'/neutral_legs'" not in JS and '"/neutral_legs"' not in JS
    assert "fetch('/move_legs'" in neutral and "method: 'POST'" in neutral
    assert "body: JSON.stringify({left_height: 50, right_height: 50, left_leg: 50, right_leg: 50, speed: 0.8})" in neutral
    assert "if (!r.ok)" in neutral and "notifyLiveError" in neutral


def test_mobile_stacked_views_and_sticky_bar():
    html = MOVEMENTS_HTML
    css = (REPO_ROOT / "src" / "www" / "static" / "css" / "movement_editor.css").read_text()
    show = function_source("showView")
    open_edit = function_source("openForEdit")
    open_create = function_source("openCreate")
    restore = function_source("restoreDraft")
    close = function_source("closeEditor")
    back = function_source("handleLibraryClick")
    assert "me-view-library" in html and "me-view-editor" in html
    assert html.index('id="meConfirmStrip"') < html.index('me-view-library')
    assert "#meConfirmStrip { grid-column: 1 / -1;" in css
    assert "function showView(view)" in show and "setAttribute('data-me-view', view)" in show
    assert "pane.scrollTop = 0;" in show and "scrollTop = 0" in show and "window.scrollTo(0, 0)" in show
    assert "showView('editor');" in open_edit and "showView('editor');" in open_create
    assert "showView('editor');" in restore
    assert "showView('library');" in close
    assert 'id="meBackBtn"' in html and "‹ Library" in html
    assert "target.closest('#meBackBtn')" in back and "closeEditor();" in back
    sticky = html.split('class="me-sticky"', 1)[1].split('</section>', 1)[0]
    for element_id in ('mePlayBtn', 'meLiveToggle', 'meNeutralBtn', 'meSaveBtn'):
        assert f'id="{element_id}"' in sticky
    assert 'data-state="CLEAN"' in sticky and "save.setAttribute('data-state', state)" in function_source("setSaveState")
    assert re.search(r"@media\s*\(max-width:\s*768px\)", css)
    assert re.search(r"@media\s*\(max-width:\s*400px\)", css)
    assert ".me-sticky .hud-btn" in css and "min-height: 44px" in css
    assert "grid-template-areas" in css and "me-servo-label" in css and "me-mode-chips" in css


def test_swipe_suppression_includes_me_editor():
    touch_start = MAIN_JS.split("tabContent.addEventListener('touchstart',", 1)[1].split("tabContent.addEventListener('touchmove',", 1)[0]
    touch_move = MAIN_JS.split("tabContent.addEventListener('touchmove',", 1)[1].split("function onTouchFinish()", 1)[0]
    selectors = re.findall(r"\.closest\('([^']+)'\)", touch_start)
    assert any(set(selector.split(', ')) == {'.me-editor', '.servo-card', '.body-speed'} for selector in selectors)
    assert "touchOnSlider = (el.tagName === 'INPUT' && el.type === 'range') ||" in touch_start
    assert "if (!isMobile || touchOnSlider) return;" in touch_move


def test_nav_guard_hooks_present_in_swipe_iife():
    touch_finish = MAIN_JS.split("function onTouchFinish()", 1)[1].split("tabContent.addEventListener('touchend',", 1)[0]
    nav_click = MAIN_JS.split("navBtns.forEach(function (btn)", 1)[1].split("// ── Core tab switching ──", 1)[0]
    assert "window._tarsGoToTab = goToTab;" in MAIN_JS
    assert MAIN_JS.index("function goToTab(index, animated)") < MAIN_JS.index("window._tarsGoToTab = goToTab;")
    assert "if (window._meTabLeaveGuard && window._meTabLeaveGuard(newIndex) === false) return;" in touch_finish
    assert "if (window._meTabLeaveGuard && window._meTabLeaveGuard(idx) === false) return;" in nav_click


def test_studio_registers_leave_guard_and_desktop_intercept():
    update_guard = function_source("updateTabLeaveGuard")
    restore_swipe = function_source("restoreSwipeTrack")
    show_confirm = function_source("showTabLeaveConfirm")
    desktop = function_source("handleDesktopTabClick")
    close = function_source("closeEditor")
    mark_dirty = function_source("markDirty")
    confirm = function_source("handleConfirmClick")
    assert "window._meTabLeaveGuard = function (nextIndex)" in update_guard
    assert "if (draft && (isDirty() || rawDirty()))" in update_guard
    # M3 audit-B F4: navigating to the ALREADY-ACTIVE tab is a no-op — never
    # prompt (matches the desktop interceptor's skip).
    assert "if (window.getCurrentTabIndex && window.getCurrentTabIndex() === nextIndex) return true;" in update_guard
    assert "pendingTabIndex = nextIndex;" in update_guard and "showTabLeaveConfirm(nextIndex);" in update_guard
    assert "restoreSwipeTrack();" in update_guard
    assert "delete window._meTabLeaveGuard;" in update_guard
    assert "return false;" in update_guard
    assert "getElementById('swipeTrack')" in restore_swipe
    assert "classList.contains('active')" in restore_swipe and "translateX(-" in restore_swipe
    assert "updateTabLeaveGuard();" in mark_dirty
    assert "el('meRawText').addEventListener('input', updateTabLeaveGuard)" in JS
    assert "pendingIndex" in show_confirm and "discard-tab-leave" in show_confirm and "keep-tab-editing" in show_confirm
    assert "strip.scrollIntoView({block: 'nearest'});" in show_confirm
    assert "getElementById('myTab')" in JS
    assert "myTab.addEventListener('click', handleDesktopTabClick, true)" in JS
    assert "if (!draft || (!isDirty() && !rawDirty())) return;" in desktop
    assert "event.preventDefault();" in desktop and "event.stopPropagation();" in desktop
    assert "showTabLeaveConfirm(nextIndex);" in desktop
    discard = confirm.split("if (action === 'discard-tab-leave')", 1)[1].split("if (action === 'keep-tab-editing')", 1)[0]
    keep = confirm.split("if (action === 'keep-tab-editing')", 1)[1].split("if (action === 'discard-unsaved')", 1)[0]
    assert "closeEditor(true);" in discard and "window._tarsGoToTab(nextIndex, true);" in discard
    assert "if (nextIndex !== null" not in discard
    assert "updateTabLeaveGuard();" in close and "setSaveState('CLEAN');" in close
    assert "clearConfirmStrip();" in keep and "delete window._meTabLeaveGuard" not in keep


def test_motion_builder_references_all_removed():
    motion_html = (TEMPLATE_ROOT / "tabs" / "motion.html").read_text()
    assert "motionBuilder" not in motion_html
    assert "builder_content" not in motion_html
    assert "bldStepList" not in MAIN_JS
    assert "'/save_sequence'" not in MAIN_JS
    assert "window.switchTab" not in MAIN_JS


def test_builder_orphan_templates_deleted():
    orphan_templates = ("builder.html", "body.html", "arms.html", "legs.html", "builder_content.html")
    for template in orphan_templates:
        assert not (TEMPLATE_ROOT / "tabs" / template).exists(), f"orphan template remains: {template}"


def test_tars_preview_js_file_unchanged():
    preview_path = REPO_ROOT / "src" / "www" / "static" / "js" / "tars_preview.js"
    # SHA-256 recorded at the frozen project baseline (8308fa9d; same blob since 9b671d6).
    expected_sha256 = "f8cf2568afa1ca652f3a48d9268c402a0ae89cec97469d1b454c05fd21b946b6"
    assert hashlib.sha256(preview_path.read_bytes()).hexdigest() == expected_sha256


def test_studio_adopts_preview_hook_contract():
    preview_ids = ("bldPreviewViewport", "bldPreview", "bldPreviewStop")
    for element_id in preview_ids:
        assert MOVEMENTS_HTML.count(f'id="{element_id}"') == 1
    include_re = re.compile(r"{%\s*include\s+'([^']+)'\s*%}")
    queue = [TEMPLATE_ROOT / "index.html"]
    included = set()
    while queue:
        path = queue.pop().resolve()
        if path in included:
            continue
        included.add(path)
        queue.extend(TEMPLATE_ROOT / name for name in include_re.findall(path.read_text()))
    template_ids = [value for path in included for value in re.findall(r'id="([^"]+)"', path.read_text())]
    for element_id in preview_ids:
        assert template_ids.count(element_id) == 1, f"{element_id} must occur once across the included templates"

    hook = re.search(r"window\._bldGetSteps\s*=\s*function\s*\(\)\s*\{([^}]+)\}", JS)
    assert hook, "Studio must define the preview data hook"
    assert "deepCopy(branchSteps(activeBranch()))" in hook.group(1)
    assert "Legs only · ref steps skipped · approximate timing" in MOVEMENTS_HTML
    assert "3D preview unavailable offline" in MOVEMENTS_HTML
    assert "typeof THREE === 'undefined'" in JS
    assert "previewButton.hidden = true" in JS
    assert 'id="mePlayNote"' in MOVEMENTS_HTML
    assert "Plays to completion — no mid-move stop in v1" in MOVEMENTS_HTML
    details = re.search(r"<details\b([^>]*)class=\"me-preview-details\"([^>]*)>", MOVEMENTS_HTML)
    assert details and " open" not in details.group(1) + details.group(2)
    preview_source = (REPO_ROOT / "src" / "www" / "static" / "js" / "tars_preview.js").read_text()
    assert "IntersectionObserver" in preview_source and "observer.observe(viewport)" in preview_source


def test_bottom_nav_label_and_aria_stability():
    button = re.search(r'<button[^>]*data-tab-index="5"[^>]*>.*?</button>', INDEX_HTML, re.S)
    assert button
    declaration = button.group(0)
    assert 'aria-label="Movements"' in declaration and "<span>Studio</span>" in declaration
    assert "<span>Move+</span>" not in INDEX_HTML


def test_webui_tabs_table_cutover():
    webui = (REPO_ROOT / "docs" / "web-ui.md").read_text()
    assert "| **builder**" not in webui
    assert "| **legs** / **arms** / **body**" not in webui
    assert "Movement Studio" in webui
    assert "GET /movements" in webui and "GET /movements/defaults" in webui
    endpoints = webui.split("## Key endpoints", 1)[1].split("\n## ", 1)[0]
    movement_route_rows = [row for row in endpoints.splitlines() if "`GET /movements`" in row]
    assert len(movement_route_rows) == 1 and "`GET /movements/defaults`" in movement_route_rows[0]


