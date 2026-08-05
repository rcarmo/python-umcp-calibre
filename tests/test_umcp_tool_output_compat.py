import json
import os
import unittest

os.environ.setdefault("CALIBRE_UMCP_DRY_RUN", "1")

from calibre_umcp.umcp import MCPServer


ROOT_ARRAY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "integer"},
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


class InferredArraySchemaServer(MCPServer):
    def tool_numbers(self) -> list[int]:
        return [1, 2, 3]


class ToolOutputCompatibilityTests(unittest.TestCase):
    def test_inferred_root_array_output_schema_is_published_with_items_wrapper(self):
        server = InferredArraySchemaServer()
        tool = next(tool for tool in server.discover_tools()["tools"] if tool["name"] == "numbers")
        self.assertEqual(tool["outputSchema"], ROOT_ARRAY_SCHEMA)

    def test_explicit_root_array_output_schema_wraps_structured_content_only(self):
        server = MCPServer()

        def numbers() -> list[int]:
            return [1, 2, 3]

        server.register_tool(
            "numbers",
            numbers,
            output_schema={"type": "array", "items": {"type": "integer"}},
        )

        tool = next(tool for tool in server.discover_tools()["tools"] if tool["name"] == "numbers")
        self.assertEqual(tool["outputSchema"], ROOT_ARRAY_SCHEMA)

        response = server.handle_tools_call(1, {"name": "numbers", "arguments": {}})
        self.assertEqual(response["result"]["structuredContent"], {"items": [1, 2, 3]})
        self.assertEqual(json.loads(response["result"]["content"][0]["text"]), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
