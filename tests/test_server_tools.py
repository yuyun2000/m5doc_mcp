import unittest
from unittest.mock import patch

import server


def emitted_event(mock_emit, event_name):
    for call in mock_emit.call_args_list:
        if call.args and call.args[0] == event_name:
            return call.kwargs
    raise AssertionError(f"event not emitted: {event_name}")


class ServerToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_list_contains_feedback(self):
        tools = await server.list_tools()
        self.assertEqual(
            [tool.name for tool in tools],
            ["knowledge_search", "knowledge_answer", "knowledge_feedback"],
        )

    async def test_search_cloud_log_contains_original_query(self):
        query = "AtomS3 CAN Unit wiring example"
        with (
            patch.object(server, "retrieve_knowledge_text", return_value={"info": "result"}),
            patch.object(server.cloud_logger, "emit", return_value=True) as emit,
        ):
            result = await server.handle_call_tool(
                "knowledge_search",
                {"query": query, "is_chip": False, "filter_type": "product"},
            )

        event = emitted_event(emit, "mcp_tool_call")
        self.assertEqual(event["input_text"], query)
        self.assertFalse(event["input_truncated"])
        self.assertIn("result", result[0].text)

    async def test_answer_cloud_log_contains_original_question(self):
        question = "Why does CoreS3 fail to initialize the camera?"
        with (
            patch.object(server, "answer_question", return_value="Check the camera bus."),
            patch.object(server.cloud_logger, "emit", return_value=True) as emit,
        ):
            result = await server.handle_call_tool(
                "knowledge_answer",
                {"question": question},
            )

        event = emitted_event(emit, "mcp_tool_call")
        self.assertEqual(event["input_text"], question)
        self.assertFalse(event["input_truncated"])
        self.assertIn("camera bus", result[0].text)

    async def test_feedback_is_marked_for_high_priority_manual_review(self):
        with patch.object(server.cloud_logger, "emit", return_value=True) as emit:
            result = await server.handle_call_tool(
                "knowledge_feedback",
                {
                    "category": "missing_documentation",
                    "feedback": "The AtomS3 CAN Unit wiring and termination example is missing.",
                    "original_question": "How should AtomS3 connect to the CAN Unit?",
                    "product": "AtomS3 CAN Unit",
                    "expected_information": "Add wiring, termination and sample code.",
                    "severity": "medium",
                    "source_tool": "knowledge_search",
                },
            )

        event = emitted_event(emit, "knowledge_feedback")
        self.assertTrue(event["manual_review"])
        self.assertEqual(event["priority"], "high")
        self.assertEqual(event["review_status"], "pending")
        self.assertEqual(event["category"], "missing_documentation")
        self.assertIn("feedback_id=", result[0].text)

    async def test_feedback_fails_closed_when_cloud_queue_is_unavailable(self):
        with patch.object(server.cloud_logger, "emit", return_value=False):
            result = await server.handle_call_tool(
                "knowledge_feedback",
                {
                    "category": "tool_error",
                    "feedback": "The knowledge search tool repeatedly returns an empty result.",
                },
            )

        self.assertIn("暂未保存", result[0].text)


if __name__ == "__main__":
    unittest.main()
