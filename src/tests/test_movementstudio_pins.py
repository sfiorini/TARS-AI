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


def test_movements_html_retains_legacy_json_editor_inside_shell():
    legacy = MOVEMENTS_HTML.split('class="body-panel me-legacy"', 1)
    assert len(legacy) == 2
    legacy_html = legacy[1].split("</div>\n</div>", 1)[0]
    for element_id in (
        "mvResetBtn", "mvPruneBtn", "mvError", "mvNewId", "mvNewBtn",
        "mvPoolList", "mvEditor", "mvEditorId", "mvName", "mvType",
        "mvDescription", "mvSteps", "mvStepsArms", "mvSaveBtn", "mvCancelBtn",
    ):
        assert legacy_html.count(f'id="{element_id}"') == 1
    assert legacy_html.count("<script>") == 1
    assert 'class="builder-wrap"' in legacy_html
    assert "function createMovement()" in legacy_html and "function saveEditor()" in legacy_html
    # Legacy inline IIFE stays BYTE-IDENTICAL to 9b671d6 until M1-S14 removes it
    # (audit M0-B F5). Hash of the <script>…</script> region, verified at HEAD.
    script = legacy_html[legacy_html.index("<script>"):legacy_html.rindex("</script>") + len("</script>")]
    assert hashlib.sha256(script.encode()).hexdigest() == (
        "f088ea1efd365a89d96e3c2cb37b13c8aa33e52b5e3c710cfcaedb468be4c2e5")


def test_index_loads_movement_editor_after_main_before_tars_preview():
    js_tag = re.search(r'<script[^>]+movement_editor\.js[^>]*></script>', INDEX_HTML)
    css_tag = re.search(r'<link[^>]+movement_editor\.css[^>]*>', INDEX_HTML)
    assert js_tag and len(re.findall(r'movement_editor\.js', INDEX_HTML)) == 1
    assert INDEX_HTML.index("js/main.js") < js_tag.start() < INDEX_HTML.index("js/tars_preview.js")
    assert css_tag and len(re.findall(r'movement_editor\.css', INDEX_HTML)) == 1
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
    editor = function_source("openLegacyEditor")
    for field_id in ("mvEditorId", "mvName", "mvType", "mvDescription", "mvSteps", "mvStepsArms"):
        assert f"el('{field_id}')" in editor
    save_bridge = function_source("handleLegacySave")
    assert "legacyEditingId === null" in save_bridge and "if (!r.ok)" in save_bridge
    assert "addEventListener('click', handleLegacySave, true)" in JS
    # Stale-bridge interlock (audit M0-B F2): identity guard + disarm on legacy
    # interaction, so a legacy-row edit can never PUT through the armed bridge.
    assert "mvEditorId').textContent !==" in save_bridge
    assert "legacyEditingId = null; return;" in save_bridge
    disarm = function_source("disarmLegacyBridge")
    assert "legacyEditingId = null" in disarm
    assert "el('mvPoolList').addEventListener('click'" in JS
    assert "el('mvNewBtn').addEventListener('click', disarmLegacyBridge, true)" in JS


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
    assert "meNewBtn" in click_handler and "mvNewId" in click_handler
    assert ".focus()" in click_handler and "scrollIntoView" in click_handler


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


