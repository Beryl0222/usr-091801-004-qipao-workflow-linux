"""事件存储：每个订单是一条只追加的事件流。

- append(stream, events, expected_version) 实现乐观并发：
  两家门店同时修改同一订单时，只有一方成功，另一方得到 VersionConflict。
- 全局 RLock 保证写入串行与读快照一致；读取返回副本，避免外部改动历史。
- 事件本身不可变（只追加），收款与工时因此无法事后篡改。
"""

import threading
from itertools import count

from .errors import VersionConflict


class EventStore:
    def __init__(self):
        self._streams = {}
        self._global_seq = count(1)
        self._lock = threading.RLock()

    def load(self, stream_id):
        """返回某订单的事件列表副本（按版本顺序）。"""
        with self._lock:
            return [dict(e) for e in self._streams.get(stream_id, [])]

    def version(self, stream_id):
        with self._lock:
            return len(self._streams.get(stream_id, []))

    def exists(self, stream_id):
        with self._lock:
            return stream_id in self._streams

    def append(self, stream_id, events, expected_version):
        """以乐观并发追加事件。

        expected_version 为调用方读取到的版本号（事件条数）；
        传 -1 表示要求流尚不存在（新建）。
        """
        if not events:
            return self.version(stream_id)
        with self._lock:
            current = len(self._streams.get(stream_id, []))
            if expected_version == -1:
                if current != 0:
                    raise VersionConflict(stream_id, expected_version, current)
            elif current != expected_version:
                raise VersionConflict(stream_id, expected_version, current)
            stream = self._streams.setdefault(stream_id, [])
            stored = []
            for event in events:
                stored.append(
                    {
                        "global_seq": next(self._global_seq),
                        "version": len(stream) + 1,
                        "event": event["type"],
                        "data": dict(event.get("data", {})),
                    }
                )
                stream.append(stored[-1])
            return len(stream)

    def stream_ids(self):
        with self._lock:
            return list(self._streams.keys())

    def all_events(self):
        """按全局序号导出全部事件（投影重放用）。"""
        with self._lock:
            items = [
                (stream_id, dict(e))
                for stream_id, stream in self._streams.items()
                for e in stream
            ]
        items.sort(key=lambda item: item[1]["global_seq"])
        return items
