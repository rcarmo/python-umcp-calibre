from calibre.customize import InterfaceActionBase


class CalibreUmcpPlugin(InterfaceActionBase):
    name = "Calibre µMCP Bridge"
    description = "Launch calibre-umcp against the active Calibre library."
    supported_platforms = ["windows", "osx", "linux"]
    author = "Rui Carmo"
    version = (0, 1, 0)
    minimum_calibre_version = (6, 0, 0)
    actual_plugin = "calibre_umcp_plugin.ui:CalibreUmcpAction"
