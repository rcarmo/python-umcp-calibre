try:
    from calibre.customize import InterfaceActionBase
except ImportError:  # Allows unit tests to import bridge modules outside Calibre.
    class InterfaceActionBase:  # type: ignore[no-redef]
        pass


PLUGIN_VERSION = (0, 1, 0)
PLUGIN_VERSION_STRING = ".".join(str(part) for part in PLUGIN_VERSION)


class CalibreUmcpPlugin(InterfaceActionBase):
    name = "Calibre µMCP Bridge"
    description = "Expose native MCP access to the active Calibre library."
    supported_platforms = ["windows", "osx", "linux"]
    author = "Rui Carmo"
    version = PLUGIN_VERSION
    minimum_calibre_version = (6, 0, 0)
    actual_plugin = "calibre_plugins.calibre_umcp_plugin.ui:CalibreUmcpAction"
