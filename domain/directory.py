"""角色目录：人员、门店、工艺、工作日历。

角色（role）：
- customer 顾客：只能操作自己的订单（故事许可、确认、支付、试衣）
- store    门店顾问：两家伦敦门店各有账号，跨店可见订单
- designer 设计师：提交稿版
- manager  工坊主管：备料、派工、验收
- artisan  绣娘/裁缝：凭技能领取任务，只能看到任务所需最小信息
"""

from datetime import date

from .errors import PermissionDenied

# 传播许可范围（故事授权），与制作授权分开管理
CONSENT_SCOPES = {
    "store_showcase": "门店实物/图片展示",
    "social_media": "社交平台公开传播",
    "marketing": "营销物料使用",
}

ROLE_LABELS = {
    "customer": "顾客",
    "store": "门店顾问",
    "designer": "设计师",
    "manager": "工坊主管",
    "artisan": "绣娘/裁缝",
}


class Calendar:
    """工作日历：weekday 工作日集合（Mon=0..Sun=6）+ 节假日 {date: 名称}。"""

    def __init__(self, working_weekdays, holidays=None):
        self.working_weekdays = set(working_weekdays)
        self.holidays = dict(holidays or {})

    def is_working(self, day):
        return day.weekday() in self.working_weekdays and day not in self.holidays

    def holiday_name(self, day):
        return self.holidays.get(day)


class Directory:
    def __init__(self):
        self.people = {}
        self.stores = {}
        self.crafts = {}
        # 每个工匠一份日历（中国节假日），门店一份日历（英国节假日）
        self.calendars = {}
        self.workshop_calendar_id = None
        self._lock = __import__("threading").RLock()

    # ---- 人员 ----
    def add_person(self, person_id, name, role, skills=None, store_id=None, tz=None):
        with self._lock:
            self.people[person_id] = {
                "id": person_id,
                "name": name,
                "role": role,
                "skills": list(skills or []),
                "store_id": store_id,
                "tz": tz,
            }
        return person_id

    def actor(self, actor_id):
        person = self.people.get(actor_id)
        if person is None:
            raise PermissionDenied(f"未知操作人：{actor_id}")
        return person

    def require_role(self, actor_id, roles):
        person = self.actor(actor_id)
        if person["role"] not in roles:
            raise PermissionDenied(
                f"{person['name']}（{ROLE_LABELS.get(person['role'], person['role'])}）无权执行此操作"
            )
        return person

    def artisans_with_skill(self, skill):
        return [p for p in self.people.values() if p["role"] == "artisan" and skill in p["skills"]]

    # ---- 门店 ----
    def add_store(self, store_id, name, tz, calendar_id):
        self.stores[store_id] = {
            "id": store_id,
            "name": name,
            "tz": tz,
            "calendar_id": calendar_id,
        }
        return store_id

    # ---- 工艺 ----
    def add_craft(self, craft_id, name, stage, base_days):
        self.crafts[craft_id] = {
            "id": craft_id,
            "name": name,
            "stage": stage,
            "base_days": base_days,
        }
        return craft_id

    # ---- 日历 ----
    def add_calendar(self, calendar_id, working_weekdays, holidays):
        self.calendars[calendar_id] = Calendar(working_weekdays, holidays)
        return calendar_id

    def set_workshop_calendar(self, calendar_id):
        """工匠统一使用工坊日历（中国节假日）。"""
        self.workshop_calendar_id = calendar_id

    def calendar_for_actor(self, person):
        """绣娘/裁缝/主管用工坊日历；门店顾问用所属门店日历。"""
        if person["role"] == "store" and person.get("store_id"):
            return self.calendars[self.stores[person["store_id"]]["calendar_id"]]
        if self.workshop_calendar_id:
            return self.calendars[self.workshop_calendar_id]
        return None

    def public(self):
        """对外目录快照。"""
        return {
            "roles": ROLE_LABELS,
            "consent_scopes": CONSENT_SCOPES,
            "stores": self.stores,
            "crafts": self.crafts,
            "people": [
                {k: v for k, v in p.items() if k != "tz"} for p in self.people.values()
            ],
            "holidays": {
                cid: {d.isoformat(): name for d, name in cal.holidays.items()}
                for cid, cal in self.calendars.items()
            },
        }
