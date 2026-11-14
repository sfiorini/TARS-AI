"""M0 Movement Studio UI string/AST pins (environment-independent, no JS execution)."""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MOVEMENTS_HTML = (REPO_ROOT / "src" / "www" / "templates" / "tabs" / "movements.html").read_text()
INDEX_HTML = (REPO_ROOT / "src" / "www" / "templates" / "index.html").read_text()
MAIN_JS = (REPO_ROOT / "src" / "www" / "static" / "js" / "main.js").read_text()
JS = (REPO_ROOT / "src" / "www" / "static" / "js" / "movement_editor.js").read_text()


def test_movements_html_is_studio_shell_with_library_contract():
    for needle in ('id="meLibSearch"', 'meLibTypeFilter', 'meLibSort', 'meLibList', 'meNewBtn', 'meResetBtn', 'mePruneBtn', 'meConfirmStrip'):
        assert needle in MOVEMENTS_HTML


def test_movements_html_retains_legacy_json_editor_inside_shell():
    for needle in ('mvResetBtn', 'mvPruneBtn', 'mvError', 'mvNewId', 'mvNewBtn', 'mvPoolList', 'mvEditor', 'mvEditorId', 'mvName', 'mvType', 'mvDescription', 'mvSteps', 'mvStepsArms', 'mvSaveBtn', 'mvCancelBtn', '<script>'):
        assert MOVEMENTS_HTML.count(needle) >= 1


def test_index_loads_movement_editor_after_main_before_tars_preview():
    js = '<script src="{{ url_for(\'static\', filename=\'js/movement_editor.js\') }}?v=7.6"></script>'
    css = '<link rel="stylesheet" href="{{ url_for(\'static\', filename=\'css/movement_editor.css\') }}?v=7.6">'
    assert js in INDEX_HTML and INDEX_HTML.count(js) == 1
    assert INDEX_HTML.index('js/main.js') < INDEX_HTML.index(js) < INDEX_HTML.index('js/tars_preview.js')
    assert css in INDEX_HTML and INDEX_HTML.index('css/main.css') < INDEX_HTML.index(css)
    assert (REPO_ROOT / "src" / "www" / "static" / "css" / "movement_editor.css").stat().st_size > 0


def test_tab_ids_still_eight_with_movements_at_index_five():
    tabs = re.search(r'const TAB_IDS = \[([^\]]+)\]', MAIN_JS).group(1)
    btns = re.search(r'const TAB_BTN_IDS = \[([^\]]+)\]', MAIN_JS).group(1)
    tab_values = re.findall(r"'([^']+)'", tabs)
    btn_values = re.findall(r"'([^']+)'", btns)
    assert len(tab_values) == 8 and tab_values[5] == 'movements'
    assert len(btn_values) == 8 and btn_values[5] == 'movements-tab'


def test_index_includes_movements_html_exactly_once():
    assert INDEX_HTML.count("tabs/movements.html") == 1


def test_boot_fetches_pool_with_error_and_retry_state():
    s = JS
    assert "fetch('/movements'" in s
    assert 'r.ok' in s and 'Failed to load movements.' in s and 'meLibRetry' in s


def test_no_silent_fetches_in_movement_editor():
    s = JS
    assert '.catch(function () {})' not in s
    assert s.count('.ok') >= s.count('fetch(')
    include_re = re.compile(r"{%\s*include\s+'([^']+)'")

    def includes(path):
        text = path.read_text()
        return [REPO_ROOT / "src" / "www" / "templates" / name for name in include_re.findall(text)]

    seen = set()
    queue = [REPO_ROOT / "src" / "www" / "templates" / "index.html"]
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        queue.extend(includes(path))
    ids = []
    for path in seen:
        ids.extend(re.findall(r'id="([^"]+)"', path.read_text()))
    assert len(ids) == len(set(ids)), sorted({x for x in ids if ids.count(x) > 1})

def test_library_row_contract_data_mid_and_summary():
    s = JS
    for needle in ('meLibRow', 'data-mid', 'data-action="play"', 'data-action="edit"', 'data-action="dup"', 'data-action="del"', "' steps · '"):
        assert needle in s


def test_library_search_filter_sort_count_wiring():
    s = JS
    for needle in ('meLibSearch', "'input'", "data-type", "value=\"id\"", "value=\"name\"", "value=\"type\"", 'No movements match.', 'sortKey', 'matchesFilters', ' of ', 'legs', 'arms'):
        assert needle in s


def test_create_button_bridges_to_legacy_create_in_m0():
    s = JS
    assert "getElementById('meNewBtn')" in s and "getElementById('mvNewId')" in s


def test_badges_use_defaults_with_degraded_mode():
    s = JS
    for needle in ("fetch('/movements/defaults'", "'default'", "'modified'", "'custom'", 'defaultsOk = false'):
        assert needle in s


def test_play_calls_play_saved_sequence_and_surfaces_errors():
    s = JS
    for needle in ("fetch('/play_saved_sequence'", 'name:', 'res.d.error'):
        assert needle in s


def test_duplicate_posts_full_copy_with_free_suffix():
    s = JS
    for needle in ("'_copy'", "'/movements'", "'POST'", 'steps:', 'steps_arms:'):
        assert needle in s


def test_delete_computes_pool_referrers_before_any_fetch():
    s = JS
    assert 'function findReferrers' in s and 'steps_arms' in s
    assert s.index('findReferrers(') < s.index("fetch('/movements/' + encodeURIComponent")


def test_delete_shipped_warning_and_no_window_confirm():
    s = JS
    for needle in ('is a shipped movement', 'is referenced by', 'Delete anyway', "encodeURIComponent(id)"):
        assert needle in s
    assert 'window.confirm' not in s
