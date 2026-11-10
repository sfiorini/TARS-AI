"""Voiceprint barge-in acceptance gate (the veto).

The gate decides whether a captured interruption is the enrolled user or
TARS's own bleed.  Two properties matter operationally:

1. The minimum speech length is CONFIGURABLE.  It was a hard-coded 8 frames
   (~1 s), so a short interruption ("stop!") was rejected before the speaker
   embedding was even computed — TARS stopped talking and then had to be
   woken again.  `[STT] bargein_min_speech_ms` (default 500) now drives it.
2. A rejection for length must not pay for an embedding; a long capture must
   actually consult the speaker ID manager.
"""
import types
import unittest
from unittest import mock

import numpy as np

import modules.module_stt as module_stt


def _manager(min_speech_ms=500, threshold=0.75):
    m = types.SimpleNamespace(
        DEBUG=False,
        MODEL_RATE=16000,          # real STTManager carries this class attr
        config={"STT": {"bargein_min_speech_ms": min_speech_ms}},
        _bargein_voiceprint_threshold=threshold,
    )
    m._bargein_min_speech_samples = (
        module_stt.STTManager._bargein_min_speech_samples.__get__(m))
    m._voiceprint_veto = module_stt.STTManager._voiceprint_veto.__get__(m)
    return m


def _sid(name="Stefano", confidence=0.90):
    sid = mock.MagicMock()
    sid.extract_embedding.return_value = np.ones(256, dtype=np.float32)
    sid.identify_speaker.return_value = (name, confidence)
    sid.get_enrolled_speakers.return_value = ["Stefano"]
    return sid


class TestMinSpeechSamples(unittest.TestCase):
    def test_default_is_500ms(self):
        m = _manager()
        self.assertEqual(m._bargein_min_speech_samples(), 8000)   # 0.5 s @ 16 kHz

    def test_config_override(self):
        self.assertEqual(_manager(min_speech_ms=1000)._bargein_min_speech_samples(), 16000)
        self.assertEqual(_manager(min_speech_ms=250)._bargein_min_speech_samples(), 4000)

    def test_invalid_value_falls_back_to_default(self):
        self.assertEqual(_manager(min_speech_ms="nonsense")._bargein_min_speech_samples(), 8000)
        self.assertEqual(_manager(min_speech_ms=-5)._bargein_min_speech_samples(), 8000)


class TestVoiceprintVeto(unittest.TestCase):
    def test_short_capture_is_rejected_without_computing_an_embedding(self):
        m = _manager(min_speech_ms=500)
        sid = _sid()
        short = np.zeros(4000, dtype=np.int16)          # 0.25 s
        with mock.patch("modules.module_speaker_id.get_speaker_id_manager", return_value=sid):
            self.assertFalse(m._voiceprint_veto(short))
        sid.extract_embedding.assert_not_called()

    def test_half_second_capture_is_evaluated(self):
        """0.5 s must now reach the speaker check (was rejected at 1 s)."""
        m = _manager(min_speech_ms=500)
        sid = _sid(confidence=0.9)
        capture = np.zeros(8000, dtype=np.int16)
        with mock.patch("modules.module_speaker_id.get_speaker_id_manager", return_value=sid):
            self.assertTrue(m._voiceprint_veto(capture))
        sid.extract_embedding.assert_called_once()

    def test_low_confidence_is_rejected(self):
        m = _manager(threshold=0.75)
        sid = _sid(confidence=0.40)
        with mock.patch("modules.module_speaker_id.get_speaker_id_manager", return_value=sid):
            self.assertFalse(m._voiceprint_veto(np.zeros(16000, dtype=np.int16)))

    def test_unknown_profile_is_rejected(self):
        m = _manager()
        sid = _sid(name="Unknown_1", confidence=0.95)
        with mock.patch("modules.module_speaker_id.get_speaker_id_manager", return_value=sid):
            self.assertFalse(m._voiceprint_veto(np.zeros(16000, dtype=np.int16)))

    def test_rejections_are_logged_with_a_reason(self):
        """A silent veto is indistinguishable from a broken feature: the user
        sees TARS stop, go Listening, then Standby, with their words gone."""
        cases = [
            ("too short", dict(min_speech_ms=1000), np.zeros(4000, dtype=np.int16), None),
            ("low confidence", dict(threshold=0.75), np.zeros(16000, dtype=np.int16),
             _sid(confidence=0.30)),
            ("unknown profile", dict(), np.zeros(16000, dtype=np.int16),
             _sid(name="Unknown_3", confidence=0.99)),
        ]
        for label, kwargs, audio, sid in cases:
            m = _manager(**kwargs)
            with mock.patch("modules.module_speaker_id.get_speaker_id_manager",
                            return_value=sid), \
                 mock.patch.object(module_stt, "queue_message") as qmsg:
                self.assertFalse(m._voiceprint_veto(audio), label)
            msgs = [c.args[0] for c in qmsg.call_args_list if c.args]
            self.assertTrue(any("barge-in rejected" in x.lower() for x in msgs),
                            f"{label}: no rejection log, got {msgs}")

    def test_missing_manager_is_rejected(self):
        m = _manager()
        with mock.patch("modules.module_speaker_id.get_speaker_id_manager", return_value=None):
            self.assertFalse(m._voiceprint_veto(np.zeros(16000, dtype=np.int16)))


if __name__ == "__main__":
    unittest.main()
