"""首批上线样例：两家伦敦门店、工坊人员、苏绣/湘绣工艺、中英节假日。

节假日直接影响产能与交期预测：
- 工坊（苏州/长沙绣娘、裁缝）按中国法定节假日与调休简化为"周末+法定假"；
- 门店按英格兰公共假日（bank holidays）。
"""

from datetime import date

from .directory import Directory

# 2026 年样例节假日（足以演示国庆/春节窗口与英国 bank holiday）
CN_HOLIDAYS_2026 = {
    date(2026, 1, 1): "元旦",
    date(2026, 2, 16): "春节初一",
    date(2026, 2, 17): "春节初二",
    date(2026, 2, 18): "春节初三",
    date(2026, 2, 19): "春节初四",
    date(2026, 2, 20): "春节初五",
    date(2026, 4, 6): "清明节",
    date(2026, 5, 1): "劳动节",
    date(2026, 6, 19): "端午节",
    date(2026, 9, 25): "中秋节",
    date(2026, 10, 1): "国庆节",
    date(2026, 10, 2): "国庆节",
    date(2026, 10, 5): "国庆节",
    date(2026, 10, 6): "国庆节",
    date(2026, 10, 7): "国庆节",
}

UK_HOLIDAYS_2026 = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 6): "Easter Monday",
    date(2026, 5, 4): "Early May Bank Holiday",
    date(2026, 5, 25): "Spring Bank Holiday",
    date(2026, 8, 31): "Summer Bank Holiday",
    date(2026, 12, 25): "Christmas Day",
    date(2026, 12, 28): "Boxing Day (substitute)",
}


def build_directory():
    d = Directory()

    d.add_calendar("cn-cal", range(5), CN_HOLIDAYS_2026)   # 工坊：周一至周五
    d.add_calendar("uk-cal", range(6), UK_HOLIDAYS_2026)   # 门店：周一至周六
    d.set_workshop_calendar("cn-cal")

    d.add_store("store-soho", "伦敦苏豪门店", "Europe/London", "uk-cal")
    d.add_store("store-shoreditch", "伦敦肖迪奇门店", "Europe/London", "uk-cal")

    d.add_person("cust-lin", "林婉清", "customer", tz="Asia/Shanghai")
    d.add_person("cust-owen", "Owen Bennett", "customer", tz="Europe/London")

    d.add_person("adv-soho", "顾问·苏豪Amy", "store", store_id="store-soho", tz="Europe/London")
    d.add_person("adv-shore", "顾问·肖迪奇Priya", "store", store_id="store-shoreditch", tz="Europe/London")

    d.add_person("des-zhao", "设计师·赵岚", "designer", tz="Asia/Shanghai")

    d.add_person("mgr-qin", "工坊主管·秦师傅", "manager", tz="Asia/Shanghai")

    # 绣娘：苏绣（精细双面绣）、湘绣（虎兽写实）技能不同，派工按技能匹配
    d.add_person("art-su-1", "绣娘·沈阿姨（苏州）", "artisan", skills=["suxiu"], tz="Asia/Shanghai")
    # 周小梅兼通苏绣与盘金钉珠，可承接与主绣并行的辅助工序
    d.add_person("art-su-2", "绣娘·周小梅（苏州）", "artisan", skills=["suxiu", "beadwork"], tz="Asia/Shanghai")
    d.add_person("art-xiang-1", "绣娘·向桂兰（长沙）", "artisan", skills=["xiangxiu"], tz="Asia/Shanghai")
    # 裁缝
    d.add_person("art-tailor-1", "裁缝·老孙", "artisan", skills=["tailor"], tz="Asia/Shanghai")
    d.add_person("art-tailor-2", "裁缝·阿珍", "artisan", skills=["tailor"], tz="Asia/Shanghai")

    # 工艺：刺绣基期最长；备料→刺绣→裁剪→缝制顺序推进，刺绣组内可并行
    d.add_craft("suxiu", "苏绣（双面精细绣）", "embroidery", base_days=12)
    d.add_craft("xiangxiu", "湘绣（写实立体绣）", "embroidery", base_days=10)
    d.add_craft("cut", "裁剪打版", "cutting", base_days=2)
    d.add_craft("tailor", "手工缝制", "sewing", base_days=6)
    d.add_craft("material", "真丝备料与检验", "material", base_days=3)
    d.add_craft("beadwork", "盘金钉珠辅助工序", "embroidery", base_days=4)

    return d
