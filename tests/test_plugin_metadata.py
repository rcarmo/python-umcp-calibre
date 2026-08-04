import unittest

from plugins.calibre_umcp_plugin import CalibreUmcpPlugin


class PluginMetadataTests(unittest.TestCase):
    def test_actual_plugin_uses_calibre_plugins_namespace(self):
        self.assertEqual(
            CalibreUmcpPlugin.actual_plugin,
            "calibre_plugins.calibre_umcp_plugin.ui:CalibreUmcpAction",
        )


if __name__ == "__main__":
    unittest.main()
