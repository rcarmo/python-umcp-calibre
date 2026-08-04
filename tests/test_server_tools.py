import os

os.environ.setdefault("CALIBRE_UMCP_DRY_RUN", "1")
os.environ.setdefault("CALIBRE_LIBRARIES", "main=/books,articles=/books/Articles")
os.environ.setdefault("CALIBRE_DEFAULT_LIBRARY", "main")

from calibre_umcp.server import CalibreMCPServer


def test_lists_libraries():
    server = CalibreMCPServer()
    assert server.tool_list_libraries()["main"] == "/books"


def test_dry_run_convert():
    server = CalibreMCPServer()
    result = server.tool_convert_book("/tmp/in.epub", "/tmp/out.pdf")
    assert result["output_path"] == "/tmp/out.pdf"
