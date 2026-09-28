import json
import math
import unittest
from types import SimpleNamespace

from app.knowledge import (
    _bm25_scores,
    _cosine,
    _load_embedding,
    _tokens,
    search_chunks,
)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _statement):
        return FakeResult(self.rows)


def row(name: str, content: str, embedding=None, page: int = 1):
    chunk = SimpleNamespace(
        content=content,
        embedding=json.dumps(embedding) if isinstance(embedding, list) else embedding,
        page=page,
    )
    document = SimpleNamespace(name=name)
    return chunk, document


class TokenizationTests(unittest.TestCase):
    def test_mixed_language_tokens_keep_words_and_chinese_bigrams(self):
        self.assertEqual(
            _tokens("HEFT 用于异构调度"),
            ["heft", "用于", "于异", "异构", "构调", "调度"],
        )

    def test_unrelated_shared_chinese_character_does_not_match(self):
        query = set(_tokens("区块链"))
        unrelated = set(_tokens("区域块状链条"))
        self.assertTrue(query.isdisjoint(unrelated))


class BM25Tests(unittest.TestCase):
    def test_rare_query_term_ranks_the_relevant_chunk_first(self):
        documents = [
            "workflow scheduling overview",
            "workflow scheduling basics",
            "workflow scheduling survey",
            "workflow scheduling tutorial",
            "workflow scheduling benchmark",
            "workflow scheduling system",
            "workflow scheduling platform",
            "workflow scheduling notes",
            "HEFT algorithm for heterogeneous processors",
        ]
        scores = _bm25_scores("HEFT workflow scheduling", documents)
        self.assertEqual(scores.index(max(scores)), 8)

    def test_chinese_query_ranks_exact_topic_over_partial_noise(self):
        documents = [
            "无人机辅助边缘计算中的任务调度方法",
            "无人机飞行控制与航线规划",
            "边缘设备资源管理",
        ]
        scores = _bm25_scores("无人机边缘计算任务调度", documents)
        self.assertEqual(scores.index(max(scores)), 0)

    def test_empty_query_has_zero_scores(self):
        self.assertEqual(_bm25_scores("--", ["some text", "其他内容"]), [0.0, 0.0])


class VectorSafetyTests(unittest.TestCase):
    def test_cosine_rejects_dimension_mismatch(self):
        self.assertEqual(_cosine([1.0, 0.0], [1.0]), 0.0)

    def test_embedding_loader_rejects_corrupt_or_non_finite_values(self):
        self.assertIsNone(_load_embedding("not-json"))
        self.assertIsNone(_load_embedding(json.dumps([1.0, math.inf])))
        self.assertIsNone(_load_embedding(json.dumps({"value": [1.0]})))


class SearchTests(unittest.TestCase):
    def test_keyword_fallback_uses_bm25_order(self):
        db = FakeSession([
            row("generic.txt", "workflow scheduling scheduling scheduling"),
            row("heft.txt", "HEFT workflow scheduling for heterogeneous processors"),
        ])
        hits = search_chunks(db, "kb", "HEFT workflow scheduling", limit=2)
        self.assertEqual([hit["document"] for hit in hits], ["heft.txt", "generic.txt"])
        self.assertTrue(all(0 < hit["score"] < 1 for hit in hits))

    def test_incompatible_index_falls_back_to_bm25(self):
        db = FakeSession([
            row("heft.txt", "HEFT workflow scheduling", embedding=[1.0]),
            row("other.txt", "weather forecast", embedding="broken"),
        ])
        hits = search_chunks(db, "kb", "HEFT", query_vector=[1.0, 0.0])
        self.assertEqual(hits[0]["document"], "heft.txt")

    def test_compatible_index_does_not_leak_keyword_noise_below_threshold(self):
        db = FakeSession([
            row("keyword-only.txt", "HEFT workflow scheduling", embedding=[0.0, 1.0]),
        ])
        hits = search_chunks(db, "kb", "HEFT", query_vector=[1.0, 0.0])
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
