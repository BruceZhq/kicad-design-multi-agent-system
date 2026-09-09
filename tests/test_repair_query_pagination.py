import pytest
from ratsnestpro.orchestration.engineering_workspace import EngineeringRequests


def test_oversized_read_returns_bounded_page_and_explicit_deferral():
    value = {'engineering_queries': [{'tool': 'pcb', 'limit': 240} for _ in range(6)]}
    batch, receipt = EngineeringRequests.bounded_repair_batch(value)
    assert len(batch.engineering_queries) == 3
    assert all(q.limit == 100 for q in batch.engineering_queries)
    assert receipt['deferred_queries'] == 3
    assert value['engineering_queries'][0]['limit'] == 240


def test_bounding_never_accepts_an_unknown_tool():
    with pytest.raises(ValueError):
        EngineeringRequests.bounded_repair_batch({'engineering_queries': [{'tool': 'shell'}]})
