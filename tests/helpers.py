"""测试场景工厂：构造一个已进入制作阶段的订单，两店共同服务。"""

from qipao import FixedClock, OrderRepository, OrderService

START = "2026-09-21T09:00:00Z"  # 周一

MEASUREMENTS_V1 = {"chest": 88, "waist": 70, "hip": 92, "length": 120,
                   "shoulder": 38}
MEASUREMENTS_V2 = {"chest": 88.5, "waist": 70, "hip": 92, "length": 120,
                   "shoulder": 38}

ELEMENTS_PEONY = [
    {"kind": "motif", "value": "peony", "label": "牡丹纹"},
    {"kind": "pattern_story", "value": "grandmother", "label": "家族传承"},
    {"kind": "thread_palette", "value": "soft_pink", "label": "柔粉丝线"},
    {"kind": "silhouette", "value": "fitted", "label": "修身"},
    {"kind": "collar", "value": "high", "label": "立领"},
    {"kind": "fastener", "value": "frog_button", "label": "盘扣"},
    {"kind": "sleeve", "value": "cap", "label": "盖袖"},
]

QUOTE_2200 = {"lines": [
    {"item": "19 姆米素绉缎面料", "amount": 420},
    {"item": "苏绣手工（牡丹纹）", "amount": 1400},
    {"item": "裁剪与缝制", "amount": 380},
]}


class V:
    """版本号持有者：每次成功写命令后自动更新。"""

    def __init__(self, version):
        self.v = version


def build_service(start=START):
    clock = FixedClock(start)
    svc = OrderService(OrderRepository(), clock=clock)
    return svc, clock


def open_order(svc, v, *, with_publication=("workshop_internal",)):
    """咨询→设计→确认→备料→派工，返回 order_id。v 为 V 包装的版本。"""
    oid, v.v = svc.create_order("p_amelia", "store_mayfair", "p_grace")
    _, v.v = svc.add_store(oid, "p_grace", "store_covent", v.v)
    _, v.v = svc.record_story(
        oid, "p_grace", "p_amelia",
        "祖母从苏州带来的牡丹绣样，希望绣在旗袍左襟。",
        "牡丹象征家族记忆与祝福，是祖母出嫁时嫁衣上的纹样。", v.v)
    _, v.v = svc.set_consent(oid, "p_amelia", True, v.v)
    for scope in with_publication:
        _, v.v = svc.set_publication(oid, "p_amelia", scope, True, v.v)
    _, v.v = svc.add_measurement(oid, "p_grace", MEASUREMENTS_V1, v.v,
                                 note="首次量体")
    _, v.v = svc.submit_draft(oid, "p_chen", ELEMENTS_PEONY, "suzhou", v.v,
                              inspiration="祖母嫁衣牡丹")
    _, v.v = svc.approve_version(
        oid, "p_amelia", f"{oid}_draft_1", 1, QUOTE_2200,
        "silk_2026_09_a", v.v)
    _, v.v = svc.record_payment(oid, "p_grace", 1100, v.v, note="定金 50%")
    _, v.v = svc.allocate_material(oid, "p_lin", "silk_2026_09_a", 3.0, v.v)
    _, v.v = svc.assign_task(oid, "p_lin", "embroidery", "p_wang", v.v)
    _, v.v = svc.assign_task(oid, "p_lin", "cutting", "p_zhao", v.v)
    return oid


def complete_production(svc, oid, v):
    """刺绣→裁剪→缝制完工，返回 (刺绣任务id, 缝制任务id)。"""
    emb = f"{oid}_embroidery_1"
    cut = f"{oid}_cutting_1"
    _, v.v = svc.start_task(oid, "p_wang", emb, v.v)
    _, v.v = svc.log_work(oid, "p_wang", emb, 42, 45, v.v, note="苏绣牡丹")
    _, v.v = svc.complete_task(oid, "p_wang", emb, v.v)
    _, v.v = svc.start_task(oid, "p_zhao", cut, v.v)
    _, v.v = svc.complete_task(oid, "p_zhao", cut, v.v)
    _, v.v = svc.assign_task(oid, "p_lin", "tailoring", "p_zhao", v.v)
    tail = f"{oid}_tailoring_1"
    _, v.v = svc.start_task(oid, "p_zhao", tail, v.v)
    _, v.v = svc.log_work(oid, "p_zhao", tail, 26, 40, v.v, note="缝制")
    _, v.v = svc.complete_task(oid, "p_zhao", tail, v.v)
    return emb, tail
