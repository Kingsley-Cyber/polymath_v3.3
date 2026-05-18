from bson import ObjectId

from models.schemas import Conversation


def test_conversation_response_preserves_trace_events():
    trace_event = {
        "id": "trace-1",
        "lane": "model_call",
        "title": "Utility web query helper",
        "status": "done",
        "content": "Utility rewrote the web query.",
        "timestamp": "2026-05-18T00:00:00.000Z",
        "metadata": {"model": "openai/glm-5-turbo"},
    }

    conversation = Conversation.model_validate(
        {
            "_id": str(ObjectId()),
            "title": "Trace test",
            "model_config": {"model": "deepseek/deepseek-v4-flash"},
            "messages": [
                {
                    "role": "assistant",
                    "content": "Done.",
                    "trace_events": [trace_event],
                }
            ],
        }
    )

    dumped = conversation.model_dump(mode="json", by_alias=True)

    assert dumped["messages"][0]["trace_events"] == [trace_event]
