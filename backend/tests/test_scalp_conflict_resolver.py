"""P0-6 ConflictResolver 单元测试"""

from __future__ import annotations

from processors.scalp_signal.conflict_resolver import (
    CandidateBundle,
    OPPOSITE_TIE_THRESHOLD,
    resolve,
)


def _bundle(name: str, direction: str, conf: int) -> CandidateBundle:
    return CandidateBundle(
        strategy_name=name,
        direction=direction,
        confidence=conf,
        candidate=object(),
        scoring=object(),
    )


class TestSingleCandidate:
    def test_single_accepted(self):
        b = _bundle("A", "up", 80)
        rep = resolve([b])
        assert len(rep.accepted) == 1
        assert rep.accepted[0].bundle is b
        assert rep.rejected == []


class TestSameDirection:
    def test_higher_confidence_wins(self):
        a = _bundle("A", "up", 80)
        b = _bundle("B", "up", 75)
        c = _bundle("C", "up", 90)
        rep = resolve([a, b, c])
        assert len(rep.accepted) == 1
        assert rep.accepted[0].bundle.strategy_name == "C"
        assert {r.bundle.strategy_name for r in rep.rejected} == {"A", "B"}
        assert all(r.reject_reason == "conflict_same_direction_lower" for r in rep.rejected)


class TestOppositeDirection:
    def test_clearly_higher_wins(self):
        up = _bundle("A", "up", 90)
        down = _bundle("B", "down", 70)
        rep = resolve([up, down])
        assert len(rep.accepted) == 1
        assert rep.accepted[0].bundle.direction == "up"
        assert rep.rejected[0].bundle.direction == "down"
        assert rep.rejected[0].reject_reason == "conflict_opposite_direction_lower"

    def test_tied_blocks_all(self):
        """top-up vs top-down 差距 < 阈值 → 全拒"""
        up = _bundle("A", "up", 80)
        down = _bundle("B", "down", 78)  # 差 2 < OPPOSITE_TIE_THRESHOLD=5
        rep = resolve([up, down])
        assert rep.accepted == []
        assert len(rep.rejected) == 2
        for r in rep.rejected:
            assert r.reject_reason == "conflict_opposite_direction_unresolved"

    def test_at_threshold_just_passes(self):
        """差距 == 阈值 → 仍解析（< 才平局）"""
        up = _bundle("A", "up", 80)
        down = _bundle("B", "down", 80 - OPPOSITE_TIE_THRESHOLD)
        rep = resolve([up, down])
        # 80 - 5 = 75，diff=5 不严格小于 5 → 解析
        assert len(rep.accepted) == 1
        assert rep.accepted[0].bundle.direction == "up"


class TestComplexScenarios:
    def test_multi_up_one_down_unresolved_top(self):
        """3 个 up + 1 个 down，top-up=78, down=76 → 全拒"""
        ups = [
            _bundle("A1", "up", 78),
            _bundle("A2", "up", 75),
            _bundle("A3", "up", 70),
        ]
        down = _bundle("B", "down", 76)  # 差 2 < 5
        rep = resolve(ups + [down])
        # 反向冲突 + 平局 → 全拒
        assert rep.accepted == []
        assert len(rep.rejected) == 4

    def test_multi_up_one_down_resolved(self):
        """3 个 up + 1 个 down，top-up=90, down=70 → up 胜，再选最高 up"""
        ups = [
            _bundle("A1", "up", 80),
            _bundle("A2", "up", 90),
            _bundle("A3", "up", 75),
        ]
        down = _bundle("B", "down", 70)
        rep = resolve(ups + [down])
        assert len(rep.accepted) == 1
        assert rep.accepted[0].bundle.strategy_name == "A2"
        assert rep.accepted[0].bundle.confidence == 90
        # 4 个被拒
        assert len(rep.rejected) == 3
        # B 是反向，A1 / A3 是同向次优
        kinds = {(r.bundle.strategy_name, r.reject_reason) for r in rep.rejected}
        assert ("B", "conflict_opposite_direction_lower") in kinds
        assert ("A1", "conflict_same_direction_lower") in kinds
        assert ("A3", "conflict_same_direction_lower") in kinds

    def test_empty_input(self):
        rep = resolve([])
        assert rep.accepted == []
        assert rep.rejected == []
