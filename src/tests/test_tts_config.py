"""Unit tests for Task 1 (TTS provider config plumbing):
- [TTS] keys in config.ini.template: tts_fallback_list, tts_timeout,
  fishaudio_model, fishaudio_reference_id, speechify_voice_id, speechify_model
- TTSConfig fields / from_config_dict / validate() in module_config
- FISH_AUDIO_API_KEY / SPEECHIFY_API_KEY read from env in load_config()
- webui CONFIG_METADATA: ttsoption options include fishaudio/speechify, new TTS fields
- device profiles allow the new providers
- .env.template keys + scripts/configure_tars_client.sh robot chain
"""
import configparser
import os
import re
import sys
import unittest
from unittest import mock


# Bootstrap gitignored src/config.ini from the template so module imports
# (load_config at import time) succeed on a fresh clone.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# Bootstrap a config.ini from the template into a TEMP dir (never the working
# tree) and point the config loader at it, so tests work on read-only checkouts.
import tempfile, shutil
_TMP = tempfile.mkdtemp(prefix="tars-test-config-")
_CONFIG_INI = os.path.join(_TMP, "config.ini")
shutil.copyfile(os.path.join(_SRC, "config.ini.template"), _CONFIG_INI)
os.environ["TARS_CONFIG_PATH"] = _CONFIG_INI

_REPO = os.path.abspath(os.path.join(_SRC, ".."))

FISH_TARS_REF = "6a57e35f5c8244dbb5d012a3c434ec16"

from modules import module_config  # noqa: E402


def _base_dict(**overrides):
    """Minimal config_dict accepted by TTSConfig.from_config_dict."""
    d = {
        "ttsoption": "piper",
        "toggle_charvoice": True,
        "tts_voice": "",
        "is_talking_override": False,
        "is_talking": False,
        "global_timer_paused": False,
    }
    d.update(overrides)
    return d


class TestTemplateTtsKeys(unittest.TestCase):
    def test_template_declares_new_tts_keys(self):
        parser = configparser.ConfigParser()
        parser.read(os.path.join(_SRC, "config.ini.template"))
        self.assertTrue(parser.has_section("TTS"))
        self.assertEqual(parser.get("TTS", "tts_fallback_list"), "external,piper,espeak")
        self.assertEqual(parser.get("TTS", "fishaudio_model"), "s2.1-pro-free")
        self.assertEqual(parser.get("TTS", "fishaudio_reference_id"), "")
        self.assertEqual(parser.get("TTS", "speechify_voice_id"), "george")
        self.assertEqual(parser.get("TTS", "speechify_model"), "simba-3.5")
        self.assertTrue(parser.getboolean("TTS", "speechify_stream"))
        self.assertEqual(parser.get("TTS", "speechify_output_format"), "pcm_24000")
        self.assertEqual(parser.getint("TTS", "tts_timeout"), 30)

    def test_template_ttsoption_comment_lists_new_providers(self):
        with open(os.path.join(_SRC, "config.ini.template"), encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r"(?m)^# TTS engine:.*$", text)
        self.assertIsNotNone(m, "template must keep a '# TTS engine:' comment above ttsoption")
        self.assertIn("fishaudio", m.group(0))
        self.assertIn("speechify", m.group(0))


class TestParseHelpers(unittest.TestCase):
    def test_parse_comma_separated(self):
        self.assertEqual(
            module_config._parse_tts_fallback_list("speechify,elevenlabs, external ,piper"),
            ["speechify", "elevenlabs", "external", "piper"],
        )

    def test_parse_json_array_tolerated(self):
        self.assertEqual(
            module_config._parse_tts_fallback_list('["speechify","piper"]'),
            ["speechify", "piper"],
        )

    def test_parse_list_passthrough(self):
        self.assertEqual(module_config._parse_tts_fallback_list(["a", " b ", ""]), ["a", "b"])

    def test_parse_empty_string_gives_empty_list(self):
        self.assertEqual(module_config._parse_tts_fallback_list(""), [])

    def test_parse_none_gives_default(self):
        self.assertEqual(
            module_config._parse_tts_fallback_list(None), ["external", "piper", "espeak"]
        )

    def test_parse_timeout(self):
        self.assertEqual(module_config._parse_tts_timeout("45"), 45.0)
        self.assertEqual(module_config._parse_tts_timeout(10), 10.0)
        self.assertEqual(module_config._parse_tts_timeout("abc"), 30.0)
        self.assertEqual(module_config._parse_tts_timeout(None), 30.0)


class TestTTSConfigFromConfigDict(unittest.TestCase):
    def test_defaults_when_keys_missing(self):
        cfg = module_config.TTSConfig.from_config_dict(_base_dict())
        self.assertEqual(cfg.tts_fallback_list, ["external", "piper", "espeak"])
        self.assertEqual(cfg.fishaudio_model, "s2.1-pro-free")
        self.assertEqual(cfg.speechify_voice_id, "george")
        self.assertEqual(cfg.speechify_model, "simba-3.5")
        self.assertIn(cfg.fishaudio_reference_id, (None, ""))
        self.assertIsNone(cfg.fishaudio_api_key)
        self.assertIsNone(cfg.speechify_api_key)
        self.assertEqual(cfg.tts_timeout, 30.0)
        # dict-style access keeps working (module_tts / webui use both styles)
        self.assertEqual(cfg["tts_fallback_list"], ["external", "piper", "espeak"])
        self.assertEqual(cfg.get("fishaudio_model"), "s2.1-pro-free")
        self.assertEqual(cfg.get("tts_timeout"), 30.0)

    def test_speechify_streaming_defaults(self):
        cfg = module_config.TTSConfig.from_config_dict(_base_dict())
        self.assertIs(cfg.speechify_stream, True)
        self.assertEqual(cfg.speechify_output_format, "pcm_24000")

    def test_speechify_stream_explicit_false_survives(self):
        # The falsy trap: an explicit False (bool or string) must reach the
        # provider as False — never `_cfg(...) or True`.
        for value in (False, "False", "false", "0"):
            cfg = module_config.TTSConfig.from_config_dict(_base_dict(speechify_stream=value))
            self.assertIs(cfg.speechify_stream, False, value)

    def test_speechify_output_format_empty_is_default_explicit_passthrough(self):
        cfg = module_config.TTSConfig.from_config_dict(_base_dict(speechify_output_format=""))
        self.assertEqual(cfg.speechify_output_format, "pcm_24000")  # empty = unset
        cfg = module_config.TTSConfig.from_config_dict(_base_dict(speechify_output_format="pcm_44100"))
        self.assertEqual(cfg.speechify_output_format, "pcm_44100")

    def test_explicit_values(self):
        cfg = module_config.TTSConfig.from_config_dict(_base_dict(
            ttsoption="fishaudio",
            tts_fallback_list="speechify,elevenlabs,external,piper,espeak",
            fishaudio_api_key="fish-test-key",
            fishaudio_model="s2.1-pro",
            fishaudio_reference_id=FISH_TARS_REF,
            speechify_api_key="sp-test-key",
            speechify_voice_id="brian",
            speechify_model="simba-turbo",
            tts_timeout="15",
        ))
        self.assertEqual(
            cfg.tts_fallback_list, ["speechify", "elevenlabs", "external", "piper", "espeak"]
        )
        self.assertEqual(cfg.fishaudio_api_key, "fish-test-key")
        self.assertEqual(cfg.fishaudio_model, "s2.1-pro")
        self.assertEqual(cfg.fishaudio_reference_id, FISH_TARS_REF)
        self.assertEqual(cfg.speechify_api_key, "sp-test-key")
        self.assertEqual(cfg.speechify_voice_id, "brian")
        self.assertEqual(cfg.speechify_model, "simba-turbo")
        self.assertEqual(cfg.tts_timeout, 15.0)

    def test_json_array_fallback_list(self):
        cfg = module_config.TTSConfig.from_config_dict(
            _base_dict(tts_fallback_list='["piper","espeak"]'))
        self.assertEqual(cfg.tts_fallback_list, ["piper", "espeak"])

    def test_empty_fallback_list_stays_empty(self):
        cfg = module_config.TTSConfig.from_config_dict(_base_dict(tts_fallback_list=""))
        self.assertEqual(cfg.tts_fallback_list, [])


class TestTTSConfigValidate(unittest.TestCase):
    def test_default_validate_true(self):
        with mock.patch.object(module_config, "queue_message") as qmsg:
            self.assertTrue(module_config.TTSConfig.from_config_dict(_base_dict()).validate())
        qmsg.assert_not_called()

    def test_elevenlabs_without_key_rejected(self):
        # existing behavior, pinned
        with mock.patch.object(module_config, "queue_message") as qmsg:
            cfg = module_config.TTSConfig.from_config_dict(_base_dict(ttsoption="elevenlabs"))
            self.assertFalse(cfg.validate())
        self.assertTrue(any("ElevenLabs API key" in c.args[0] for c in qmsg.call_args_list))

    def test_invalid_timeout_rejected(self):
        for bad in ("0", "-5"):
            with mock.patch.object(module_config, "queue_message") as qmsg:
                cfg = module_config.TTSConfig.from_config_dict(_base_dict(tts_timeout=bad))
                self.assertFalse(cfg.validate(), f"tts_timeout={bad} must be rejected")
            self.assertTrue(any("tts_timeout" in c.args[0] for c in qmsg.call_args_list))

    def test_non_numeric_timeout_falls_back_to_default_and_validates(self):
        cfg = module_config.TTSConfig.from_config_dict(_base_dict(tts_timeout="abc"))
        self.assertEqual(cfg.tts_timeout, 30.0)
        with mock.patch.object(module_config, "queue_message"):
            self.assertTrue(cfg.validate())


class TestLoadConfigEnvKeys(unittest.TestCase):
    def setUp(self):
        self._old_cache = module_config._config_cache
        self.addCleanup(self._restore_cache)

    def _restore_cache(self):
        module_config._config_cache = self._old_cache

    def _reload(self, env, clear=False):
        # Keep the session's config-path pointer even under clear=True: the
        # conftest points TARS_CONFIG_PATH at the isolated template copy, and
        # wiping it would make load_config() read the ambient src/config.ini.
        # clear=True stays hermetic for every other env var (API keys etc.).
        env = dict(env)
        if "TARS_CONFIG_PATH" in os.environ:
            env.setdefault("TARS_CONFIG_PATH", os.environ["TARS_CONFIG_PATH"])
        with mock.patch.dict(os.environ, env, clear=clear):
            module_config._config_cache = None
            return module_config.load_config()

    def test_env_keys_read_into_tts_config(self):
        cfg = self._reload({"FISH_AUDIO_API_KEY": "fish-test-key",
                            "SPEECHIFY_API_KEY": "sp-test-key"})
        tts = cfg["TTS"]
        self.assertEqual(tts.fishaudio_api_key, "fish-test-key")
        self.assertEqual(tts.speechify_api_key, "sp-test-key")

    def test_missing_env_keys_are_none(self):
        # Hermetic: clear ALL ambient env so a real key on the host cannot leak in.
        cfg = self._reload({}, clear=True)
        tts = cfg["TTS"]
        self.assertIsNone(tts.fishaudio_api_key)
        self.assertIsNone(tts.speechify_api_key)

    def test_load_config_exposes_new_keys_with_defaults(self):
        cfg = self._reload({}, clear=True)
        tts = cfg["TTS"]
        self.assertEqual(tts.tts_fallback_list, ["external", "piper", "espeak"])
        self.assertEqual(tts.fishaudio_model, "s2.1-pro-free")
        self.assertEqual(tts.speechify_voice_id, "george")
        self.assertEqual(tts.speechify_model, "simba-3.5")
        self.assertEqual(tts.tts_timeout, 30.0)
        self.assertIn(tts.fishaudio_reference_id, (None, ""))
        # S-M2-02 ini-read pin: load_config() must expose both new keys with
        # the template's defaults (True / pcm_24000).
        self.assertIs(tts.speechify_stream, True)
        self.assertEqual(tts.speechify_output_format, "pcm_24000")

    def test_load_config_reads_speechify_keys_from_ini(self):
        # S-M2-06 fix: pin the ini READ, not just the fallbacks. The template
        # ships True / pcm_24000 — identical to load_config()'s fallbacks — so
        # reading defaults proves nothing: a typo'd key name in the ini read
        # (module_config.py) would silently fall back and still pass. Assert
        # against values that DIFFER from the fallbacks so a typo yields the
        # fallback and fails here.
        ini = os.path.join(_TMP, "config-nondefault.ini")
        with open(_CONFIG_INI, encoding="utf-8") as fh:
            text = fh.read()
        text = text.replace("speechify_stream = True", "speechify_stream = False")
        text = text.replace("speechify_output_format = pcm_24000",
                            "speechify_output_format = pcm_44100")
        with open(ini, "w", encoding="utf-8") as fh:
            fh.write(text)
        cfg = self._reload({"TARS_CONFIG_PATH": ini}, clear=True)
        tts = cfg["TTS"]
        self.assertIs(tts.speechify_stream, False)
        self.assertEqual(tts.speechify_output_format, "pcm_44100")


class TestWebuiMetadata(unittest.TestCase):
    def test_ttsoption_options_include_new_providers(self):
        opts = module_config.CONFIG_METADATA["TTS"]["ttsoption"]["options"]
        for provider in ("espeak", "piper", "silero", "elevenlabs", "openai",
                         "other", "external", "fishaudio", "speechify"):
            self.assertIn(provider, opts)

    def test_new_tts_fields_in_schema_and_template(self):
        schema = module_config.CONFIG_METADATA["TTS"]
        parser = configparser.ConfigParser()
        parser.read(os.path.join(_SRC, "config.ini.template"))
        for key in ("tts_fallback_list", "tts_timeout", "fishaudio_model",
                    "fishaudio_reference_id", "speechify_voice_id", "speechify_model"):
            self.assertIn(key, schema, f"webui schema missing {key}")
            # webui save is template-driven: every schema key must exist in the template
            self.assertTrue(parser.has_option("TTS", key), f"template missing {key}")

    def test_parsed_tts_keys_exposed_in_schema_and_template(self):
        """B5: fishaudio_stream / fishaudio_latency / tts_volume are parsed by
        load_config and used by the providers but were missing from the webui
        schema — every parsed+used [TTS] key must be viewable/settable."""
        schema = module_config.CONFIG_METADATA["TTS"]
        parser = configparser.ConfigParser()
        parser.read(os.path.join(_SRC, "config.ini.template"))
        for key in ("fishaudio_stream", "fishaudio_latency", "tts_volume",
                    "speechify_stream", "speechify_output_format"):
            self.assertIn(key, schema, f"webui schema missing {key}")
            self.assertTrue(parser.has_option("TTS", key), f"template missing {key}")

    def test_speechify_stream_description_matches_measured_latency(self):
        """P3: the webui tooltip claimed 'TARS starts speaking in ~0.1 s'.
        Measured on the robot, the server's first byte is ≈ 0.105 s but the
        first playable ~1 s slice lands ≈ 0.9 s in (the client coalesces raw
        PCM into ~1 s slices) — the description must distinguish the two.
        """
        desc = module_config.CONFIG_METADATA["TTS"]["speechify_stream"]["description"]
        self.assertNotIn("starts speaking in ~0.1 s", desc)
        self.assertIn("0.1 s", desc)     # server first byte (measured ≈ 0.105 s)
        self.assertIn("0.9 s", desc)     # first playable slice (measured 0.929 s)

    def test_elevenlabs_model_options_include_v3(self):
        """B5: the code special-cases v3 model ids ('v3' in model_id) — the
        webui options list must offer a v3 model, not just v2-era ids."""
        opts = module_config.CONFIG_METADATA["TTS"]["elevenlabs_model"]["options"]
        self.assertIn("eleven_v3", opts)
        self.assertIn("eleven_multilingual_v2", opts)  # existing options preserved


class TestDeviceProfiles(unittest.TestCase):
    def test_all_profiles_allow_new_providers(self):
        for profile, caps in module_config.DEVICE_PROFILES.items():
            self.assertIn("fishaudio", caps.allowed_tts, str(profile))
            self.assertIn("speechify", caps.allowed_tts, str(profile))


class TestEnvTemplate(unittest.TestCase):
    def test_env_template_has_new_keys(self):
        with open(os.path.join(_REPO, ".env.template"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIsNotNone(re.search(r'(?m)^FISH_AUDIO_API_KEY=""\s*$', text))
        self.assertIsNotNone(re.search(r'(?m)^SPEECHIFY_API_KEY=""\s*$', text))


class TestConfigureScriptChain(unittest.TestCase):
    def test_robot_chain_set_to_fishaudio_first(self):
        path = os.path.join(_REPO, "scripts", "configure_tars_client.sh")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("set_kv TTS ttsoption fishaudio", text)
        self.assertIn(
            "set_kv TTS tts_fallback_list speechify,elevenlabs,external,piper,espeak", text)
        # Task 0 voice config stays on the robot
        self.assertIn(f"set_kv TTS fishaudio_reference_id {FISH_TARS_REF}", text)
        # The script must never write a literal API key into .env/config.ini
        self.assertIsNone(
            re.search(r'(FISH_AUDIO_API_KEY|SPEECHIFY_API_KEY)\s*=\s*"[^"$][^"]*"', text),
            "script must not hardcode an API key",
        )


if __name__ == "__main__":
    unittest.main()
