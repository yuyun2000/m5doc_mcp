import json
import unittest

import httpx2

import server


class MCPV2ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_modern_discover_legacy_initialize_and_transport_security(self):
        transport = httpx2.ASGITransport(app=server.app)
        base_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Host": "mcp.m5stack.com",
        }
        modern_headers = {
            **base_headers,
            "MCP-Protocol-Version": "2026-07-28",
            "MCP-Method": "server/discover",
        }
        modern_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {
                        "name": "m5doc-test",
                        "version": "1.0",
                    },
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        }
        legacy_body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "legacy-test", "version": "1.0"},
            },
        }

        async with (
            server.mcp.session_manager.run(),
            httpx2.AsyncClient(
                transport=transport,
                base_url="https://mcp.m5stack.com",
            ) as client,
        ):
            modern = await client.post("/mcp", json=modern_body, headers=modern_headers)
            legacy = await client.post("/mcp", json=legacy_body, headers=base_headers)
            modern_list = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/list",
                    "params": modern_body["params"],
                },
                headers={**modern_headers, "MCP-Method": "tools/list"},
            )
            legacy_list = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                headers={**base_headers, "MCP-Protocol-Version": "2025-11-25"},
            )
            legacy_message = await client.post(
                "/messages?session_id=missing",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=base_headers,
            )
            invalid_host = await client.post(
                "/mcp",
                json=legacy_body,
                headers={**base_headers, "Host": "evil.example"},
            )
            invalid_origin = await client.post(
                "/mcp",
                json=legacy_body,
                headers={**base_headers, "Origin": "https://evil.example"},
            )
            oversized = await client.post(
                "/mcp",
                content=json.dumps({**modern_body, "padding": "x" * (4 * 1024 * 1024)}),
                headers=modern_headers,
            )

        self.assertEqual(modern.status_code, 200)
        self.assertIn("2026-07-28", modern.json()["result"]["supportedVersions"])
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.json()["result"]["protocolVersion"], "2025-11-25")
        expected_tools = ["knowledge_search", "knowledge_answer", "knowledge_feedback"]
        self.assertEqual(
            [tool["name"] for tool in modern_list.json()["result"]["tools"]],
            expected_tools,
        )
        self.assertEqual(
            [tool["name"] for tool in legacy_list.json()["result"]["tools"]],
            expected_tools,
        )
        self.assertIn("outputSchema", modern_list.json()["result"]["tools"][0])
        self.assertNotEqual(legacy_message.status_code, 307)
        self.assertNotIn("location", legacy_message.headers)
        self.assertEqual(invalid_host.status_code, 421)
        self.assertEqual(invalid_origin.status_code, 403)
        self.assertEqual(oversized.status_code, 413)

    def test_legacy_and_streamable_routes_are_present(self):
        paths = {getattr(route, "path", "") for route in server.starlette_app.routes}
        self.assertTrue({"/sse", "/messages", "/mcp", "/health"}.issubset(paths))


if __name__ == "__main__":
    unittest.main()
