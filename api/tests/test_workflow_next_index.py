from app import workflow as wf


def test_next_index_counts_steps(client):  # client fixture triggers ensure_schema
    run_id = wf.create_run("conv-x", None, None, "ws-x", "hi")
    assert wf.next_index(run_id) == 0
    wf.add_step(run_id, 0, "respond", "生成回答", "llm")
    assert wf.next_index(run_id) == 1
    wf.add_step(run_id, 1, "tool", "调用工具：x", "feishu")
    assert wf.next_index(run_id) == 2
