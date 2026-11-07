"""C11: set_kv in scripts/configure_tars_client.sh must survive sed metacharacters.

The old implementation interpolated key/value into a sed s|...|...|
command: a value containing | & / \\ (e.g. a URL, or a sox FX chain)
corrupted the substitution or matched the wrong lines, and regex
metacharacters in the key (.) matched unrelated keys. The rewrite must do
literal matching. These tests extract the actual set_kv function from the
script and run it against a scratch config, so they exercise the real
code, not a copy.
"""
import os
import re
import subprocess
import tempfile
import unittest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SCRIPT = os.path.join(_REPO, "scripts", "configure_tars_client.sh")

_FUNC_RE = re.compile(r"(?ms)^set_kv\(\) \{.*?^\}")

_BASE_CONFIG = """\
[LLM]
base_url = http://localhost:11434
api_key = keep-me

[TTS]
ttsoption = piper
ttsurl = http://old-server:5678

[STT]
stt_processor = elevenlabs
"""


def _run_set_kv(config_path, section, key, value):
    """Extract set_kv from the script and run it against config_path."""
    with open(_SCRIPT, encoding="utf-8") as fh:
        text = fh.read()
    m = _FUNC_RE.search(text)
    if m is None:
        raise AssertionError("set_kv() not found in configure_tars_client.sh")
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(m.group(0))
        fh.write('\nset_kv "$KV_SECTION" "$KV_KEY" "$KV_VALUE"\n')
        driver = fh.name
    try:
        env = dict(os.environ, CONFIG=config_path,
                   KV_SECTION=section, KV_KEY=key, KV_VALUE=value)
        proc = subprocess.run(["bash", driver], env=env,
                              capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(driver)
    return proc


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestSetKvLiteral(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = os.path.join(self._tmp.name, "config.ini")
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write(_BASE_CONFIG)

    def _set(self, section, key, value):
        proc = _run_set_kv(self.config, section, key, value)
        self.assertEqual(proc.returncode, 0,
                         f"set_kv failed: stderr={proc.stderr!r}")
        return _read(self.config)

    def test_value_with_sed_metacharacters_replaced_literally(self):
        # | & / \ all break a sed s|...|...| substitution
        url = r"http://a|b&c/d\e:5678"
        text = self._set("TTS", "ttsurl", url)
        self.assertIn(f"ttsurl = {url}", text)
        self.assertNotIn("old-server", text)

    def test_key_with_regex_metacharacters_replaced_literally(self):
        # put the weird key INSIDE [TTS] (appending at EOF would land in [STT])
        text = _read(self.config).replace(
            "ttsoption = piper\n", "ttsoption = piper\nwe|rd.key = old-value\n")
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write(text)
        text = self._set("TTS", "we|rd.key", "new-value")
        self.assertIn("we|rd.key = new-value", text)
        self.assertNotIn("old-value", text)

    def test_section_scoped_key_does_not_touch_other_sections(self):
        # [LLM] also has a base_url which must NOT change
        text = self._set("VISION", "base_url", "http://vision:9000")
        self.assertIn("base_url = http://vision:9000", text)
        self.assertIn("base_url = http://localhost:11434", text)

    def test_missing_key_inserted_after_section_header(self):
        text = self._set("TTS", "fishaudio_model", "s2.1-pro-free")
        lines = text.splitlines()
        header = lines.index("[TTS]")
        self.assertEqual(lines[header + 1], "fishaudio_model = s2.1-pro-free")
        # the rest of the section is preserved
        self.assertIn("ttsoption = piper", text)

    def test_missing_section_appended_at_eof(self):
        text = self._set("RAG", "embedding_source", "external")
        self.assertIn("\n[RAG]\nembedding_source = external\n", text)

    def test_repeated_run_is_idempotent(self):
        self._set("TTS", "ttsurl", "http://x:1")
        text = self._set("TTS", "ttsurl", "http://x:1")
        self.assertEqual(text.count("ttsurl = http://x:1"), 1)


if __name__ == "__main__":
    unittest.main()
