import math
from datetime import UTC, datetime

from memory.long_term import RetrievedMemory, _hash_embedding, render_memory_context


def test_hash_embedding_is_stable_and_normalized() -> None:
    first = _hash_embedding("偏好: 使用毫米", 384)
    second = _hash_embedding("偏好: 使用毫米", 384)
    assert first == second
    assert len(first) == 384
    assert math.isclose(sum(value * value for value in first), 1.0, rel_tol=1e-6)


def test_rendered_memory_keeps_provenance_and_not_hidden_instructions() -> None:
    rendered = render_memory_context(
        [
            RetrievedMemory(
                memory_id="00000000-0000-0000-0000-000000000001",
                memory_type="user_fact",
                memory_key="language",
                summary="language: 中文",
                source_type="user_statement",
                occurred_at=datetime(2026, 8, 21, tzinfo=UTC),
                score=0.91,
                same_project=False,
            )
        ]
    )
    assert '"source":"user_statement"' in rendered
    assert '"score":0.91' in rendered
    assert '"summary":"language: 中文"' in rendered
