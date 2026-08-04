import unittest
from pathlib import Path

from plugins.calibre_umcp_plugin import CalibreUmcpPlugin, PLUGIN_VERSION_STRING
from plugins.calibre_umcp_plugin.bridge import BRIDGE_VERSION


class PluginMetadataTests(unittest.TestCase):
    def test_actual_plugin_uses_calibre_plugins_namespace(self):
        self.assertEqual(
            CalibreUmcpPlugin.actual_plugin,
            "calibre_plugins.calibre_umcp_plugin.ui:CalibreUmcpAction",
        )

    def test_bridge_version_matches_plugin_metadata(self):
        self.assertEqual(BRIDGE_VERSION, PLUGIN_VERSION_STRING)
        self.assertEqual(BRIDGE_VERSION, ".".join(str(part) for part in CalibreUmcpPlugin.version))

    def test_ui_status_reports_version_and_auth_state(self):
        ui_source = Path("plugins/calibre_umcp_plugin/ui.py").read_text()
        self.assertIn("BRIDGE_VERSION", ui_source)
        self.assertIn("Auth: {'enabled' if self._auth_enabled else 'disabled'}", ui_source)


if __name__ == "__main__":
    unittest.main()
