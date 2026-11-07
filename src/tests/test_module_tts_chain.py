"""Unit tests for Task 4 — the ordered provider fallback chain in module_tts.py.

Covers the plan's Review Focus items:
  1. quota exhausted on primary (QuotaExhausted) => chain moves to the next
     provider AND logs the reason; speech still plays from the next provider
  2. all cloud providers down => local piper, then espeak; never silent
     zero-chunks without a log line
  3. unknown provider name (ttsoption or list entry) => skipped with a
     warning, chain continues
  4. duplicates (ttsoption also in tts_fallback_list) => deduped, provider
     tried exactly once
  5. provider yields chunks then dies mid-stream => chunks already produced
     are kept, no mid-utterance failover (pinned behavior)
  6. empty text / missing API key => provider skipped before any HTTP call
     (no key = no request, no key in logs)
  7. 429-style RateLimited on every provider => each tried once (no infinite
     loop) + per-utterance deadline so TARS cannot hang

Plus: chain order, espeak force-appended last, comma-string list parsing,
per-provider timeout from config, is_wakeword/emotion forwarding, and
unchanged public signatures of generate_tts_audio / play_audio_chunks /
SentenceTTSPipeline.
"""
import asyncio
import inspect
import io
import os
import sys
import types
import unittest
from unittest import mock


# Bootstrap gitignored src/config.ini from the template so module imports
# (load_config at import time) succeed on a fresh clone.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_INI = os.path.join(_SRC, "config.ini")
if not os.path.exists(_CONFIG_INI):
    _TEMPLATE = os.path.join(_SRC, "config.ini.template")
    try:
        import shutil
        shutil.copyfile(_TEMPLATE, _CONFIG_INI)
    except OSError as exc:
        raise RuntimeError(
            "Cannot bootstrap src/config.ini from src/config.ini.template "
            f"(required for module imports): {exc}"
        ) from exc

from modules import module_tts  # noqa: E402
from modules import module_fishaudio  # noqa: E402
from modules.module_fishaudio import (  # noqa: E402
    QuotaExhausted,
    RateLimited,
    AuthFailed,
    TransientError,
)


def _collect(agen):
    """Drive an async generator to completion; return (chunks, exception)."""
    async def run():
        chunks = []
        try:
            async for chunk in agen:
                chunks.append(chunk)
            return chunks, None
        except Exception as exc:
            return chunks, exc
    return asyncio.run(run())


def _fake_adapter(name, order=None, chunks=0, exc=None, sleep=0.0, kwargs_rec=None):
    """Build a _PROVIDERS-style adapter: fn(text, is_wakeword, emotion) -> agen.

    chunks: number of BytesIO chunks to yield; exc: raised AFTER the chunks
    (mid-stream) or immediately when chunks == 0; sleep: async delay first.
    """
    calls = []

    async def gen(text, is_wakeword=False, emotion=None, **kwargs):
        calls.append({"text": text, "is_wakeword": is_wakeword,
                      "emotion": emotion, "kwargs": kwargs})
        if order is not None:
            order.append(name)
        if sleep:
            await asyncio.sleep(sleep)
        for i in range(chunks):
            yield io.BytesIO(("%s-chunk-%d" % (name, i)).encode("ascii"))
        if exc is not None:
            raise exc

    def adapter(text, is_wakeword, emotion):
        return gen(text, is_wakeword, emotion)

    adapter.calls = calls
    adapter.name = name
    return adapter


def _cfg(fallback, timeout=5.0):
    return {"TTS": types.SimpleNamespace(
        tts_fallback_list=fallback, tts_timeout=timeout, ttsurl="", tts_voice="")}


def _run_chain(text, ttsoption, providers, fallback, timeout=5.0, **gen_kwargs):
    """Run generate_tts_audio with patched CONFIG/_PROVIDERS/queue_message.

    Returns (chunks, exception, qmsg_mock, providers_dict).
    """
    with mock.patch.object(module_tts, "CONFIG", _cfg(fallback, timeout)), \
         mock.patch.object(module_tts, "_PROVIDERS", providers), \
         mock.patch.object(module_tts, "queue_message") as qmsg:
        chunks, exc = _collect(module_tts.generate_tts_audio(text, ttsoption, **gen_kwargs))
    return chunks, exc, qmsg, providers


def _logs(qmsg):
    return [str(c.args[0]) for c in qmsg.call_args_list if c.args]


class TestEffectiveChain(unittest.TestCase):
    def _chain(self, ttsoption, fallback):
        with mock.patch.object(module_tts, "CONFIG", _cfg(fallback)):
            return module_tts._effective_chain(ttsoption)

    def test_espeak_always_appended_last(self):
        self.assertEqual(self._chain("piper", []), ["piper", "espeak"])

    def test_dedup_first_occurrence_wins(self):
        self.assertEqual(
            self._chain("piper", ["piper", "espeak", "piper", " external "]),
            ["piper", "espeak", "external"])

    def test_comma_separated_string_list_parsed(self):
        self.assertEqual(
            self._chain("fishaudio", "speechify, piper ,espeak"),
            ["fishaudio", "speechify", "piper", "espeak"])

    def test_espeak_primary_not_duplicated(self):
        self.assertEqual(self._chain("espeak", []), ["espeak"])

    def test_names_normalized_lowercase_stripped(self):
        self.assertEqual(self._chain("  Piper ", [" ESPEAK "]), ["piper", "espeak"])

    def test_dict_section_config_supported(self):
        with mock.patch.object(module_tts, "CONFIG",
                               {"TTS": {"tts_fallback_list": ["speechify"], "tts_timeout": 3}}):
            self.assertEqual(module_tts._effective_chain("fishaudio"),
                             ["fishaudio", "speechify", "espeak"])

    def test_template_default_list_used_without_patch(self):
        # Real CONFIG (bootstrapped from config.ini.template): default
        # tts_fallback_list = "external,piper,espeak".
        chain = module_tts._effective_chain("fishaudio")
        self.assertEqual(chain, ["fishaudio", "external", "piper", "espeak"])
        self.assertEqual(chain[-1], "espeak")
        self.assertEqual(chain.count("espeak"), 1)


class TestChainRunner(unittest.TestCase):
    def test_chain_order_first_provider_with_audio_wins(self):
        order = []
        prov = {
            "a": _fake_adapter("a", order, chunks=1),
            "b": _fake_adapter("b", order, chunks=1),
            "espeak": _fake_adapter("espeak", order, chunks=1),
        }
        chunks, exc, qmsg, _ = _run_chain("Hello there.", "a", prov, ["b"])
        self.assertIsNone(exc)
        self.assertEqual(order, ["a"])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read(), b"a-chunk-0")

    def test_quota_on_primary_fails_over_and_logs_reason(self):
        prov = {
            "fishaudio": _fake_adapter("fishaudio", exc=QuotaExhausted("no credit", status_code=402)),
            "speechify": _fake_adapter("speechify", chunks=2),
            "espeak": _fake_adapter("espeak", chunks=1),
        }
        chunks, exc, qmsg, prov = _run_chain("Pay me.", "fishaudio", prov, ["speechify"])
        self.assertIsNone(exc)
        self.assertEqual(len(prov["fishaudio"].calls), 1)
        self.assertEqual(len(prov["speechify"].calls), 1)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].read(), b"speechify-chunk-0")
        logs = _logs(qmsg)
        self.assertTrue(any("fishaudio" in l and "failing over" in l and "QuotaExhausted" in l
                            and "speechify" in l for l in logs),
                        f"failover reason must be logged, got: {logs}")

    def test_all_cloud_down_then_piper_then_espeak(self):
        order = []
        prov = {
            "fishaudio": _fake_adapter("fishaudio", order, exc=QuotaExhausted("402")),
            "speechify": _fake_adapter("speechify", order, exc=RateLimited("429 after retry")),
            "external": _fake_adapter("external", order, exc=TransientError("conn refused")),
            "piper": _fake_adapter("piper", order, exc=AuthFailed("401")),
            "espeak": _fake_adapter("espeak", order, chunks=1),
        }
        chunks, exc, qmsg, _ = _run_chain(
            "All systems nominal.", "fishaudio", prov, ["speechify", "external", "piper"])
        self.assertIsNone(exc)
        self.assertEqual(order, ["fishaudio", "speechify", "external", "piper", "espeak"])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read(), b"espeak-chunk-0")
        logs = _logs(qmsg)
        # every failover logged with its reason
        for name in ["fishaudio", "speechify", "external", "piper"]:
            self.assertTrue(any(name in l and "fail" in l.lower() for l in logs),
                            f"missing failover log for {name}: {logs}")

    def test_total_failure_not_silent(self):
        prov = {
            "a": _fake_adapter("a", exc=TransientError("boom")),
            "espeak": _fake_adapter("espeak", exc=TransientError("boom2")),
        }
        chunks, exc, qmsg, _ = _run_chain("Nothing works.", "a", prov, [])
        self.assertIsNone(exc)  # outer log-and-swallow preserved
        self.assertEqual(chunks, [])
        self.assertTrue(any("No TTS provider in chain" in l for l in _logs(qmsg)),
                        f"zero-chunk outcome must be logged: {_logs(qmsg)}")

    def test_unknown_ttsoption_skipped_with_warning(self):
        prov = {"espeak": _fake_adapter("espeak", chunks=1)}
        chunks, exc, qmsg, _ = _run_chain("Hi.", "bogus", prov, ["espeak"])
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(any("unknown" in l and "bogus" in l for l in _logs(qmsg)),
                        f"unknown provider must warn: {_logs(qmsg)}")

    def test_unknown_name_in_fallback_list_skipped(self):
        order = []
        prov = {
            "a": _fake_adapter("a", order, exc=QuotaExhausted("x")),
            "b": _fake_adapter("b", order, chunks=1),
            "espeak": _fake_adapter("espeak", order, chunks=1),
        }
        # "nosuch" is deliberately NOT in the providers registry
        chunks, exc, qmsg, _ = _run_chain("Hi.", "a", prov, ["nosuch", "b"])
        self.assertIsNone(exc)
        self.assertEqual(order, ["a", "b"])
        self.assertTrue(any("unknown" in l and "nosuch" in l for l in _logs(qmsg)))

    def test_unavailable_module_skipped_with_warning(self):
        def none_adapter(text, is_wakeword, emotion):
            return None  # module not loaded on this device
        prov = {
            "silero": none_adapter,
            "espeak": _fake_adapter("espeak", chunks=1),
        }
        chunks, exc, qmsg, _ = _run_chain("Hi.", "silero", prov, [])
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(any("not available" in l and "silero" in l for l in _logs(qmsg)),
                        f"unavailable provider must warn: {_logs(qmsg)}")

    def test_dedup_provider_not_tried_twice(self):
        prov = {
            "a": _fake_adapter("a", exc=QuotaExhausted("x")),
            "b": _fake_adapter("b", chunks=1),
            "espeak": _fake_adapter("espeak", chunks=1),
        }
        chunks, exc, qmsg, _ = _run_chain("Hi.", "a", prov, ["a", "b", "a"])
        self.assertIsNone(exc)
        self.assertEqual(len(prov["a"].calls), 1, "duplicate chain entries must not re-call the provider")
        self.assertEqual(len(chunks), 1)

    def test_empty_text_no_provider_called(self):
        prov = {
            "a": _fake_adapter("a", chunks=1),
            "espeak": _fake_adapter("espeak", chunks=1),
        }
        chunks, exc, qmsg, _ = _run_chain("   ", "a", prov, [])
        self.assertIsNone(exc)
        self.assertEqual(chunks, [])
        self.assertEqual(prov["a"].calls, [])
        self.assertEqual(prov["espeak"].calls, [])
        self.assertTrue(any("empty text" in l for l in _logs(qmsg)))

    def test_mid_stream_failure_keeps_chunks_no_failover(self):
        # Pinned behavior: once a provider yielded audio, a later error ends the
        # utterance — chunks already produced are kept, next provider NOT called.
        prov = {
            "a": _fake_adapter("a", chunks=2, exc=TransientError("stream died")),
            "b": _fake_adapter("b", chunks=1),
            "espeak": _fake_adapter("espeak", chunks=1),
        }
        chunks, exc, qmsg, _ = _run_chain("Long sentence here.", "a", prov, ["b"])
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(prov["b"].calls, [])
        self.assertTrue(any("mid-stream" in l and "a" in l for l in _logs(qmsg)),
                        f"mid-stream failure must be logged: {_logs(qmsg)}")

    def test_rate_limited_everywhere_tried_once_no_infinite_loop(self):
        order = []
        prov = {
            "a": _fake_adapter("a", order, exc=RateLimited("429")),
            "b": _fake_adapter("b", order, exc=RateLimited("429")),
            "espeak": _fake_adapter("espeak", order, exc=RateLimited("429")),
        }
        chunks, exc, qmsg, _ = _run_chain("Hi.", "a", prov, ["b"])  # espeak force-appended
        self.assertIsNone(exc)
        self.assertEqual(order, ["a", "b", "espeak"])
        for name in ["a", "b", "espeak"]:
            self.assertEqual(len(prov[name].calls), 1)
        self.assertEqual(chunks, [])

    def test_per_utterance_deadline_stops_chain(self):
        # timeout 0.05 * len(chain)=3 => deadline 0.15s; provider "a" stalls 0.3s.
        prov = {
            "a": _fake_adapter("a", sleep=0.3, chunks=0),
            "b": _fake_adapter("b", chunks=1),
            "espeak": _fake_adapter("espeak", chunks=1),
        }
        chunks, exc, qmsg, _ = _run_chain("Hi.", "a", prov, ["b"], timeout=0.05)
        self.assertIsNone(exc)
        self.assertEqual(len(prov["a"].calls), 1)
        self.assertEqual(prov["b"].calls, [], "deadline must stop the chain before the next provider")
        self.assertEqual(chunks, [])
        self.assertTrue(any("deadline" in l for l in _logs(qmsg)),
                        f"deadline must be logged: {_logs(qmsg)}")

    def test_deadline_not_hit_normally(self):
        prov = {
            "a": _fake_adapter("a", chunks=1),
            "espeak": _fake_adapter("espeak", chunks=1),
        }
        chunks, exc, qmsg, _ = _run_chain("Hi.", "a", prov, [], timeout=5.0)
        self.assertIsNone(exc)
        self.assertEqual(len(chunks), 1)
        self.assertFalse(any("deadline" in l for l in _logs(qmsg)))


class TestRealProviderWiring(unittest.TestCase):
    """Exercise the REAL _PROVIDERS adapters by patching the provider globals."""

    def _run_real(self, text, ttsoption, fallback, timeout=5.0, **gen_kwargs):
        with mock.patch.object(module_tts, "CONFIG", _cfg(fallback, timeout)), \
             mock.patch.object(module_tts, "queue_message") as qmsg:
            chunks, exc = _collect(module_tts.generate_tts_audio(text, ttsoption, **gen_kwargs))
        return chunks, exc, qmsg

    def test_missing_api_key_makes_no_http_request(self):
        espeak = _fake_adapter("espeak", chunks=1)
        with mock.patch.object(module_tts, "text_to_speech_with_pipelining_fishaudio",
                               module_fishaudio.text_to_speech_with_fishaudio), \
             mock.patch.object(module_tts, "text_to_speech_with_pipelining_espeak", None), \
             mock.patch.object(module_tts, "_PROVIDERS", {
                 "fishaudio": module_tts._prov_fishaudio,
                 "espeak": espeak,
             }), \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(module_fishaudio.requests, "post") as post, \
             mock.patch.object(module_fishaudio, "queue_message"):
            chunks, exc, qmsg = self._run_real("Hello.", "fishaudio", ["espeak"])
        self.assertIsNone(exc)
        self.assertEqual(post.call_count, 0, "no API key => no HTTP request")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read(), b"espeak-chunk-0")

    def test_cloud_provider_gets_timeout_from_config(self):
        recorded = {}

        async def fake_fish(text, reference_id=None, model=None, timeout=None, **kw):
            recorded["timeout"] = timeout
            yield io.BytesIO(b"fish-wav")

        with mock.patch.object(module_tts, "text_to_speech_with_pipelining_fishaudio", fake_fish):
            chunks, exc, qmsg = self._run_real("Hello.", "fishaudio", [], timeout=7.5)
        self.assertIsNone(exc)
        self.assertEqual(recorded.get("timeout"), 7.5)
        self.assertEqual(len(chunks), 1)

    def test_elevenlabs_receives_is_wakeword(self):
        recorded = {}

        async def fake_el(text, is_wakeword):
            recorded["is_wakeword"] = is_wakeword
            yield io.BytesIO(b"el-wav")

        with mock.patch.object(module_tts, "text_to_speech_with_pipelining_elevenlabs", fake_el):
            chunks, exc, qmsg = self._run_real("Wake up.", "elevenlabs", [], is_wakeword=True)
        self.assertIsNone(exc)
        self.assertIs(recorded.get("is_wakeword"), True)
        self.assertEqual(len(chunks), 1)

    def test_piper_receives_emotion(self):
        recorded = {}

        async def fake_piper(text, emotion=None):
            recorded["emotion"] = emotion
            yield io.BytesIO(b"piper-wav")

        with mock.patch.object(module_tts, "text_to_speech_with_pipelining_piper", fake_piper):
            chunks, exc, qmsg = self._run_real("I am happy.", "piper", [], emotion="joy")
        self.assertIsNone(exc)
        self.assertEqual(recorded.get("emotion"), "joy")
        self.assertEqual(len(chunks), 1)

    def test_chain_replaces_old_hardcoded_ladder_order(self):
        # ttsoption=elevenlabs + template-style list => elevenlabs, external,
        # piper, espeak (the old hardcoded ladder, now config-driven).
        order = []

        def named(name, chunks=0, exc=None):
            async def gen(text, *a, **kw):
                order.append(name)
                for i in range(chunks):
                    yield io.BytesIO(b"%s-%d" % (name.encode(), i))
                if exc is not None:
                    raise exc
            return gen

        async def fake_external(text, timeout=None):
            order.append("external")
            raise TransientError("server down")
            yield  # pragma: no cover — presence makes this an async generator

        with mock.patch.object(module_tts, "text_to_speech_with_pipelining_elevenlabs",
                               named("elevenlabs", exc=QuotaExhausted("429 quota body"))), \
             mock.patch.object(module_tts, "_external_tts_chunks", fake_external), \
             mock.patch.object(module_tts, "text_to_speech_with_pipelining_piper",
                               named("piper", exc=TransientError("no model"))), \
             mock.patch.object(module_tts, "text_to_speech_with_pipelining_espeak",
                               named("espeak", chunks=1)):
            chunks, exc, qmsg = self._run_real("Status report.", "elevenlabs", ["external", "piper"])
        self.assertIsNone(exc)
        self.assertEqual(order, ["elevenlabs", "external", "piper", "espeak"])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read(), b"espeak-0")

    def test_external_chunks_uses_timeout_param_and_config_default(self):
        cfg = _cfg([], timeout=4.0)
        cfg["TTS"].ttsurl = "http://tts.local:5678"
        with mock.patch.object(module_tts, "CONFIG", cfg), \
             mock.patch.object(module_tts.requests, "post") as post, \
             mock.patch.object(module_tts, "queue_message"):
            resp = mock.MagicMock(status_code=200)
            resp.content = b"RIFFxx"
            post.return_value = resp
            chunks, exc = _collect(module_tts._external_tts_chunks("hi", timeout=3))
            self.assertIsNone(exc)
            self.assertEqual(post.call_args.kwargs.get("timeout"), 3)
            self.assertEqual(len(chunks), 1)
            chunks, exc = _collect(module_tts._external_tts_chunks("hi"))
            self.assertEqual(post.call_args.kwargs.get("timeout"), 4.0)


class TestPublicSignaturesUnchanged(unittest.TestCase):
    def test_generate_tts_audio_signature(self):
        sig = inspect.signature(module_tts.generate_tts_audio)
        self.assertEqual(
            list(sig.parameters),
            ["text", "ttsoption", "is_wakeword", "ttsurl", "toggle_charvoice", "tts_voice", "emotion"])
        self.assertEqual(sig.parameters["is_wakeword"].default, False)
        self.assertIsNone(sig.parameters["ttsurl"].default)
        self.assertEqual(sig.parameters["toggle_charvoice"].default, True)
        self.assertIsNone(sig.parameters["tts_voice"].default)
        self.assertIsNone(sig.parameters["emotion"].default)
        self.assertTrue(inspect.isasyncgenfunction(module_tts.generate_tts_audio))

    def test_play_audio_chunks_signature(self):
        sig = inspect.signature(module_tts.play_audio_chunks)
        self.assertEqual(list(sig.parameters), ["text", "config", "is_wakeword", "emotion"])
        self.assertEqual(sig.parameters["is_wakeword"].default, False)
        self.assertIsNone(sig.parameters["emotion"].default)

    def test_sentence_pipeline_api_intact(self):
        for attr in ["start", "feed", "finish", "join", "set_emotion",
                     "interrupted", "duration", "play_time", "remainder"]:
            self.assertTrue(hasattr(module_tts.SentenceTTSPipeline, attr), attr)

    def test_typed_errors_single_hierarchy(self):
        from modules import module_speechify
        self.assertIs(module_speechify.QuotaExhausted, module_fishaudio.QuotaExhausted)
        self.assertIs(module_tts.QuotaExhausted, module_fishaudio.QuotaExhausted)
        for cls in [QuotaExhausted, RateLimited, AuthFailed, TransientError]:
            self.assertTrue(issubclass(cls, module_tts.TTSProviderError))


class TestModuleDocstringDocumentsFallbackChain(unittest.TestCase):
    """C5: docstring drift — the module header must mention the multi-provider
    fallback chain ([ttsoption] + tts_fallback_list + espeak, first-wins)."""

    def test_header_mentions_chain_and_espeak_last_resort(self):
        doc = module_tts.__doc__ or ""
        self.assertIn("tts_fallback_list", doc)
        self.assertIn("espeak", doc.lower())
        self.assertIn("chain", doc.lower())


if __name__ == "__main__":
    unittest.main()
