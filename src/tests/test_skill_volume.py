"""M3-S3: volume ALSA platform message via which-check (D22).

shutil.which("amixer") is None → clear platform message for ALL actions.
amixer present → unchanged behavior (mock subprocess).

No real ALSA, no device access.
"""

import unittest
from unittest import mock


# -- Helpers -----------------------------------------------------------

def _import_vol_module():
    """Import skill_volume, clearing cached imports first."""
    import sys
    for key in list(sys.modules.keys()):
        if "skill_volume" in key:
            del sys.modules[key]
    import src.skills.skill_volume as sv
    return sv


# -- Tests -------------------------------------------------------------

class TestVolumePlatformMessage(unittest.TestCase):
    """S3 pin: which-check at top of execute → platform message or
    normal behavior."""

    def setUp(self):
        self.sv = _import_vol_module()

    # ---- 1. no amixer → platform message for ALL actions ------------

    def test_no_amixer_platform_message_all_actions(self):
        """shutil.which('amixer') → None → same platform message for
        get, set, increase, decrease."""
        with mock.patch("shutil.which", return_value=None):
            for action in ("get", "set", "increase", "decrease"):
                result = self.sv.execute(
                    {"action": action, "value": 50},
                    {},
                )
                self.assertIn("Volume control needs ALSA", result)
                self.assertIn("amixer", result)
                self.assertIn("platform", result)

    # ---- 2. amixer present → unchanged behavior ---------------------

    def test_amixer_present_behavior_unchanged(self):
        """shutil.which → path; mock the volume controller; set works."""
        with mock.patch("shutil.which", return_value="/usr/bin/amixer"):
            with mock.patch.object(self.sv._VolumeControl, "set_volume",
                                   return_value=True):
                result = self.sv.execute(
                    {"action": "set", "value": 50},
                    {},
                )
                self.assertIn("Volume set to 50%", result)

    # ---- 3. which-check precedes _get_vc ----------------------------

    def test_which_check_precedes_vc(self):
        """which None → _get_vc NEVER called (check at top, not buried
        in one branch)."""
        with mock.patch("shutil.which", return_value=None):
            with mock.patch("src.skills.skill_volume._get_vc") as mock_vc:
                result = self.sv.execute(
                    {"action": "get"},
                    {},
                )
                mock_vc.assert_not_called()
                self.assertIn("Volume control needs ALSA", result)


if __name__ == "__main__":
    unittest.main()