"""订单仓储：事件流的加载、折叠与乐观并发提交。"""

from .errors import NotFound
from .events import EventStore
from .order import OrderAggregate, fold


class OrderRepository:
    def __init__(self, store=None):
        self.store = store or EventStore()

    def create(self, order_id, events):
        """以 expected_version=-1 创建；订单号已存在则 VersionConflict。"""
        return self.store.append(order_id, events, -1)

    def state(self, order_id):
        if not self.store.exists(order_id):
            raise NotFound(f"订单 {order_id} 不存在")
        return fold(self.store.load(order_id))

    def version(self, order_id):
        if not self.store.exists(order_id):
            raise NotFound(f"订单 {order_id} 不存在")
        return self.store.version(order_id)

    def mutate(self, order_id, decision, expected_version=None):
        """加载→折叠→决策→追加。

        decision(aggregate) 返回事件列表。
        expected_version 缺省取读取时版本（先查再改的标准用法）；
        两家门店并发同改时，失败者收到 VersionConflict。
        """
        events = self.store.load(order_id)
        if not events and not self.store.exists(order_id):
            raise NotFound(f"订单 {order_id} 不存在")
        if expected_version is None:
            expected_version = len(events)
        state = fold(events)
        new_events = decision(OrderAggregate(state))
        version = self.store.append(order_id, new_events, expected_version)
        return {"version": version, "events": new_events, "state": fold(self.store.load(order_id))}

    def list_orders(self):
        return self.store.stream_ids()
