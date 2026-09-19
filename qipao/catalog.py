"""工艺路线、角色技能、节假日与样例注册表。

流程为有向无环图：刺绣与裁剪可并行，缝制同时依赖二者。
每道工序给出标准工作日工期与所需技能；工匠的技能系数会伸缩工期。
"""

from dataclasses import dataclass, field
from datetime import date


# -- 角色 -------------------------------------------------------------------

ROLES = (
    "customer",       # 顾客
    "designer",       # 设计师
    "store_staff",    # 门店顾问
    "embroiderer",    # 绣娘
    "tailor",         # 裁缝
    "manager",        # 工坊主管
)

# 工序 -> 可承担该工序的角色
STAGE_ROLES = {
    "consultation": ("store_staff",),
    "design": ("designer",),
    "material_prep": ("manager", "store_staff"),
    "embroidery": ("embroiderer",),
    "cutting": ("tailor",),
    "tailoring": ("tailor",),
    "fitting": ("store_staff",),
    "delivery": ("store_staff",),
}


# -- 工艺路线 ---------------------------------------------------------------
# work_days 为一名技能系数 1.0 的工匠独立完成所需工作日；
# embroidery 的实际工期还随绣种（苏绣/湘绣）复杂度调整。

PROCESS_TEMPLATE = [
    {
        "stage": "consultation",
        "name": "咨询",
        "skill": "consultation",
        "work_days": 1.0,
        "deps": [],
    },
    {
        "stage": "design",
        "name": "设计确认",
        "skill": "design",
        "work_days": 2.0,
        "deps": ["consultation"],
    },
    {
        "stage": "material_prep",
        "name": "备料",
        "skill": "material",
        "work_days": 2.0,
        "deps": ["design"],
    },
    {
        "stage": "embroidery",
        "name": "刺绣",
        "skill": "embroidery",
        "work_days": 6.0,
        "deps": ["material_prep"],
        "parallel_group": "make",
    },
    {
        "stage": "cutting",
        "name": "裁剪",
        "skill": "cutting",
        "work_days": 1.0,
        "deps": ["material_prep"],
        "parallel_group": "make",
    },
    {
        "stage": "tailoring",
        "name": "缝制",
        "skill": "tailoring",
        "work_days": 4.0,
        "deps": ["embroidery", "cutting"],
    },
    {
        "stage": "fitting",
        "name": "试衣",
        "skill": "fitting",
        "work_days": 1.0,
        "deps": ["tailoring"],
    },
    {
        "stage": "delivery",
        "name": "交付",
        "skill": "delivery",
        "work_days": 1.0,
        "deps": ["fitting"],
    },
]

PROCESS_BY_STAGE = {item["stage"]: item for item in PROCESS_TEMPLATE}

# 绣种复杂度系数（作用于刺绣标准工期）
EMBROIDERY_STYLES = {
    "suzhou": {"name": "苏绣", "factor": 1.15},
    "hunan": {"name": "湘绣", "factor": 1.0},
}


# -- 工作日历 ---------------------------------------------------------------

CALENDARS = {
    "cn_workshop": {"name": "中国工坊", "zone": "Asia/Shanghai", "weekend": (5, 6)},
    "london_store": {"name": "伦敦门店", "zone": "Europe/London", "weekend": (5, 6)},
}

# 节假日（按日历所在地本地日期，整日不排产/不计入确认缓冲）
HOLIDAYS = {
    "cn_workshop": {
        date(2026, 9, 25): "中秋节",
        date(2026, 10, 1): "国庆节",
        date(2026, 10, 2): "国庆节",
        date(2026, 10, 3): "国庆节",
        date(2026, 10, 4): "国庆节",
        date(2026, 10, 5): "国庆节",
        date(2026, 10, 6): "国庆节",
        date(2026, 10, 7): "国庆节",
        date(2027, 2, 6): "春节",
        date(2027, 2, 7): "春节",
        date(2027, 2, 8): "春节",
        date(2027, 2, 9): "春节",
        date(2027, 2, 10): "春节",
        date(2027, 2, 11): "春节",
        date(2027, 2, 12): "春节",
    },
    "london_store": {
        date(2026, 12, 25): "Christmas Day",
        date(2026, 12, 26): "Boxing Day",
        date(2026, 12, 28): "Summer/Christmas bank holiday",
    },
}


# -- 人员与样例注册表 --------------------------------------------------------

@dataclass
class Person:
    id: str
    name: str
    role: str
    skills: dict = field(default_factory=dict)  # skill -> 效率系数（越大越快）
    calendar: str = "cn_workshop"
    store_id: str | None = None
    # 并行工序容量：同一时段可并行承担的工序数（绣娘通常只绣一件）
    capacity: int = 1


REGISTRY = {
    "stores": {
        "store_mayfair": {
            "id": "store_mayfair",
            "name": "梅费尔旗袍工作室",
            "zone": "Europe/London",
            "calendar": "london_store",
        },
        "store_covent": {
            "id": "store_covent",
            "name": "考文特花园旗袍工作室",
            "zone": "Europe/London",
            "calendar": "london_store",
        },
    },
    "people": {
        "p_amelia": Person(
            "p_amelia", "Amelia Brooks", "customer",
            calendar="london_store", store_id="store_mayfair",
        ),
        "p_priya": Person(
            "p_priya", "Priya Nair", "customer",
            calendar="london_store", store_id="store_covent",
        ),
        "p_chen": Person(
            "p_chen", "陈彦宁", "designer",
            skills={"design": 1.0, "consultation": 0.8},
        ),
        "p_wang": Person(
            "p_wang", "王雪梅", "embroiderer",
            skills={"embroidery": 1.1},  # 苏绣师傅，快于标准
        ),
        "p_li": Person(
            "p_li", "李秀英", "embroiderer",
            skills={"embroidery": 0.8},  # 湘绣，针法细密但速度低于标准系数
        ),
        "p_zhao": Person(
            "p_zhao", "赵建国", "tailor",
            skills={"cutting": 1.0, "tailoring": 1.0},
        ),
        "p_sun": Person(
            "p_sun", "孙丽华", "tailor",
            skills={"cutting": 0.9, "tailoring": 0.9},
        ),
        "p_lin": Person(
            "p_lin", "林掌柜", "manager",
            skills={"material": 1.0},
        ),
        "p_grace": Person(
            "p_grace", "Grace 顾问", "store_staff",
            skills={"consultation": 1.0, "fitting": 1.0, "delivery": 1.0},
            calendar="london_store", store_id="store_mayfair",
        ),
        "p_olivia": Person(
            "p_olivia", "Olivia 顾问", "store_staff",
            skills={"consultation": 1.0, "fitting": 1.0, "delivery": 1.0},
            calendar="london_store", store_id="store_covent",
        ),
    },
    # 真丝面料批次
    "fabric_batches": {
        "silk_2026_09_a": {
            "id": "silk_2026_09_a",
            "fabric": "19 姆米素绉缎（桑蚕丝）",
            "meters": 12.0,
            "received": "2026-09-10",
            "scrapped": False,
        },
        "silk_2026_09_b": {
            "id": "silk_2026_09_b",
            "fabric": "22 姆米提花织锦缎",
            "meters": 8.0,
            "received": "2026-09-12",
            "scrapped": False,
        },
    },
}


def person(person_id):
    return REGISTRY["people"].get(person_id)


def store(store_id):
    return REGISTRY["stores"].get(store_id)
