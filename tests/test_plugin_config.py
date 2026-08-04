import importlib
import sys
import types
import unittest
from unittest.mock import patch


class FakeJSONConfig(dict):
    stores = {}

    def __init__(self, name):
        self.name = name
        self.defaults = {}
        super().__init__(self.stores.setdefault(name, {}))

    def __getitem__(self, key):
        return self.get(key, self.defaults.get(key))

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.stores[self.name][key] = value

    def commit(self):
        return None


class PluginConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        calibre = types.ModuleType("calibre")
        utils = types.ModuleType("calibre.utils")
        config_module = types.ModuleType("calibre.utils.config")
        config_module.JSONConfig = FakeJSONConfig
        cls.modules = {
            "calibre": calibre,
            "calibre.utils": utils,
            "calibre.utils.config": config_module,
        }
        cls.module_patch = patch.dict(sys.modules, cls.modules)
        cls.module_patch.start()
        sys.modules.pop("plugins.calibre_umcp_plugin.config", None)
        cls.config_module = importlib.import_module("plugins.calibre_umcp_plugin.config")

    @classmethod
    def tearDownClass(cls):
        cls.module_patch.stop()

    def setUp(self):
        FakeJSONConfig.stores.clear()

    def test_ui_token_and_explicit_switch_enable_mutations(self):
        prefs = self.config_module.config()
        prefs["token"] = "ui-secret"
        prefs["mutations_enabled"] = True
        prefs["import_roots"] = "/imports/one\n/imports/two"
        settings = self.config_module.load_settings(environ={})
        self.assertTrue(settings.ui_token_configured)
        self.assertTrue(settings.mutations_enabled)
        self.assertEqual(settings.token, "ui-secret")
        self.assertEqual(settings.import_roots, ("/imports/one", "/imports/two"))

    def test_environment_only_token_cannot_enable_mutations(self):
        prefs = self.config_module.config()
        prefs["mutations_enabled"] = True
        settings = self.config_module.load_settings(environ={"CALIBRE_UMCP_BRIDGE_TOKEN": "environment"})
        self.assertEqual(settings.token, "environment")
        self.assertFalse(settings.ui_token_configured)
        self.assertFalse(settings.mutations_enabled)

    def test_different_environment_override_disables_ui_mutation_policy(self):
        prefs = self.config_module.config()
        prefs["token"] = "ui-secret"
        prefs["mutations_enabled"] = True
        settings = self.config_module.load_settings(environ={"CALIBRE_UMCP_BRIDGE_TOKEN": "override"})
        self.assertEqual(settings.token, "override")
        self.assertTrue(settings.ui_token_configured)
        self.assertFalse(settings.mutations_enabled)


if __name__ == "__main__":
    unittest.main()
