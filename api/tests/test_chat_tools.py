from app import workflow as wf


def test_tool_call_becomes_a_step(client):  # client fixture triggers ensure_schema
    """events() 消费 stream_agent 的 tool_call/tool_result 时，落一个 tool step。"""
    run_id = wf.create_run("conv-t", None, None, "ws-t", "发个飞书")
    before = wf.next_index(run_id)
    sid = wf.add_step(run_id, before, "tool", "调用工具：send_feishu_message", "feishu",
                      input_data={"args": "{}", "access": "write"})
    wf.finish_step(sid, "succeeded", output={"result": "已发送"})
    assert wf.next_index(run_id) == before + 1
