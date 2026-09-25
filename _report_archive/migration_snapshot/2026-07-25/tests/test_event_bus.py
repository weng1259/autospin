"""EventBus pub/sub 单元测试（骨干覆盖）。"""
from __future__ import annotations

import queue

from src.event_bus import Event, EventBus


def test_subscribe_publish_receive() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    e = Event(event_id="1", method="test", phase="started")
    bus.publish(e)
    got = sub.get(timeout=0.1)
    assert got is e


def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    bus.unsubscribe(sub)
    bus.publish(Event(event_id="1", method="test", phase="started"))
    try:
        sub.get_nowait()
        raise AssertionError("should not receive after unsubscribe")
    except queue.Empty:
        pass


def test_recent_returns_last_n_in_order() -> None:
    bus = EventBus()
    events = [
        Event(event_id=f"id-{i}", method="m", phase="completed")
        for i in range(5)
    ]
    for e in events:
        bus.publish(e)
    recent = bus.recent(3)
    assert [e.event_id for e in recent] == ["id-2", "id-3", "id-4"]


def test_recent_history_bounded_by_max() -> None:
    bus = EventBus(history_max=3)
    for i in range(10):
        bus.publish(Event(event_id=f"id-{i}", method="m", phase="completed"))
    recent = bus.recent(100)
    assert len(recent) == 3
    assert [e.event_id for e in recent] == ["id-7", "id-8", "id-9"]


def test_slow_subscriber_drops_oldest_on_full_queue() -> None:
    """慢订阅者队列满 → 丢最旧那条再 put 新的（永不 block publisher）。"""
    bus = EventBus(queue_max=2)
    sub = bus.subscribe()
    for i in range(5):
        bus.publish(Event(event_id=f"id-{i}", method="m", phase="completed"))
    # 队列最多 2 条，应只留最后 2 条
    drained: list[str] = []
    while True:
        try:
            drained.append(sub.get_nowait().event_id)
        except queue.Empty:
            break
    assert len(drained) == 2
    assert drained[-1] == "id-4"


def test_unsubscribe_unknown_queue_is_noop() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    bus.unsubscribe(sub)
    # 再 unsub 不应 crash
    bus.unsubscribe(sub)


def test_event_new_id_is_unique() -> None:
    ids = {Event.new_id() for _ in range(100)}
    assert len(ids) == 100
