"""线程安全仓储：每单一把互斥锁 + 乐观版本号。

两家门店同时改同一订单时：
- 命令必须携带其最后读到的 expected_version；
- 提交在订单锁内检查版本，先提交者成功并推进版本，
  后提交者因版本过期得到 ConflictError（HTTP 409），
  必须重新读取后再决定是否合并，绝不允许后写静默覆盖先写。
"""

import threading

from .errors import ConflictError, NotFoundError


class OrderRepository:
    def __init__(self):
        self._orders = {}
        self._locks = {}
        self._guard = threading.Lock()

    def _lock_for(self, order_id):
        with self._guard:
            lock = self._locks.get(order_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[order_id] = lock
            return lock

    def create(self, order):
        with self._guard:
            if order.id in self._orders:
                raise ConflictError(f"订单已存在：{order.id}")
            self._orders[order.id] = order
        return order

    def get(self, order_id):
        with self._guard:
            order = self._orders.get(order_id)
        if order is None:
            raise NotFoundError(f"订单不存在：{order_id}")
        return order

    def exists(self, order_id):
        with self._guard:
            return order_id in self._orders

    def list_ids(self):
        with self._guard:
            return list(self._orders.keys())

    def mutate(self, order_id, expected_version, fn):
        """在订单锁内执行命令并做乐观版本检查。

        fn 接收订单聚合，返回值原样透传；命令内部追加事件会推进 version，
        因此保存后 stored 版本与对象一致。
        """
        order = self.get(order_id)
        lock = self._lock_for(order_id)
        with lock:
            if expected_version is not None \
                    and int(expected_version) != order.version:
                raise ConflictError(
                    "订单已被其他门店更新，请重新读取后再提交",
                    details={
                        "expected_version": int(expected_version),
                        "current_version": order.version,
                    },
                )
            result = fn(order)
            return result, order.version

    def snapshot(self, order_id):
        """只读快照（不加锁的浅读取用于查询；版本号随响应返回）。"""
        return self.get(order_id)
