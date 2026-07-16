from concurrent.futures import ThreadPoolExecutor

import pytest

from app.runtime_contract import RuntimeAccessDenied, RuntimeTransition
from app.runtime_events import InMemoryRuntimeEventRepository


def test_event_sequence_is_strict_under_concurrent_appends_and_replays_from_cursor():
    repository = InMemoryRuntimeEventRepository()

    def append(index: int):
        return repository.append(run_id="run", workspace_id="ws", transition=RuntimeTransition(event_type="token", payload={"index": index}))

    with ThreadPoolExecutor(max_workers=12) as pool:
        events = list(pool.map(append, range(80)))
    assert sorted(event.sequence for event in events) == list(range(1, 81))
    replay = repository.replay(run_id="run", workspace_id="ws", after_sequence=77)
    assert [event.sequence for event in replay] == [78, 79, 80]


def test_event_repository_is_non_disclosing_across_workspaces():
    repository = InMemoryRuntimeEventRepository()
    repository.append(run_id="run", workspace_id="ws-a", transition=RuntimeTransition(event_type="run.started"))
    with pytest.raises(RuntimeAccessDenied):
        repository.replay(run_id="run", workspace_id="ws-b")
