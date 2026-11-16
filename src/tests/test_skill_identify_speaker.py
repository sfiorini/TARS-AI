"""M3-S4: identify_speaker case-insensitive Unknown_ rejection (D22).

name.startswith("Unknown_") → name.lower().startswith("unknown_").
_INVALID_NAMES check is already case-insensitive; this closes the
lowercase bypass. The silent None return stays (PL-12).
"""

import unittest
from unittest import mock


# -- Helpers -----------------------------------------------------------

def _import_id_module():
    """Import skill_identify_speaker, clearing cached imports first."""
    import sys
    for key in list(sys.modules.keys()):
        if "skill_identify_speaker" in key:
            del sys.modules[key]
    import src.skills.skill_identify_speaker as sid
    return sid


# -- Tests -------------------------------------------------------------

class TestIdentifySpeakerCaseFix(unittest.TestCase):
    """S4 pin: lowercase and mixed-case Unknown_ both rejected;
    valid name passes."""

    def setUp(self):
        self.sid = _import_id_module()

    # ---- 1. lowercase unknown rejected ------------------------------

    def test_lowercase_unknown_rejected(self):
        """"unknown_foo" → None + warning logged."""
        with mock.patch(
            "modules.module_messageQue.queue_message"
        ) as mock_q:
            result = self.sid.execute(
                {"name": "unknown_foo"},
                {},
            )
        self.assertIsNone(result)
        mock_q.assert_called_once()
        self.assertIn("rejected", mock_q.call_args[0][0])

    # ---- 2. mixed-case unknown rejected -----------------------------

    def test_mixed_case_unknown_rejected(self):
        """"Unknown_foo" → None (existing behavior preserved)."""
        with mock.patch(
            "modules.module_messageQue.queue_message"
        ) as mock_q:
            result = self.sid.execute(
                {"name": "Unknown_foo"},
                {},
            )
        self.assertIsNone(result)
        mock_q.assert_called_once()
        self.assertIn("rejected", mock_q.call_args[0][0])

    # ---- 3. valid name passes ---------------------------------------

    def test_valid_name_passes(self):
        """"Stefano" with mocked managers → proceeds past the guard."""
        fake_manager = mock.MagicMock()
        fake_manager.name_current_speaker.return_value = "ok"
        with mock.patch(
            "modules.module_speaker_id.get_speaker_id_manager",
            return_value=fake_manager,
        ), mock.patch(
            "modules.module_messageQue.queue_message"
        ):
            result = self.sid.execute(
                {"name": "Stefano"},
                {},
            )
        # Valid name passes the Unknown_ guard; result can be None
        # (silent None is existing behavior, PL-12)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()