# 跨文化旗袍定制流转

连接伦敦两家门店、设计工作室与中国刺绣/缝制工坊的定制业务后端：
从社交平台咨询到交付，统一管理顾客故事与授权、量体版本、设计稿、
真丝面料批次、工序派工、返工、试衣、费用工时与交期预测。

纯 Python 标准库实现（`http.server` + `zoneinfo`），无第三方依赖；
所有时刻以 UTC 存储，按顾客/工坊所在地时区展示与排产。

## 解决的核心问题

1. **门店承诺与工坊产能对不上** — 派工必须满足工序依赖与工匠容量，
   交期预测按技能效率、并行工序、周末与节假日推算，并给出关键路径与延期归因。
2. **顾客故事的文化含义需要被尊重** — "记录授权 / 制作授权 / 传播许可"三类
   授权分离：传播许可按范围授予、可随时撤回，撤回立即对所有读模型生效；
   已确认的制作合同与已发生的工时收款不受传播撤回影响。
3. **确认后不能偷换制作版本** — 顾客确认关键元素、尺寸、报价、面料后生成
   SHA-256 指纹版本；任何改动必须走变更单，顾客再次确认新费用与新日期，
   旧版本指纹永久留档、新旧版本串成取代链。
4. **绣娘与裁缝只需最小信息** — 工匠视图只含自己的在制任务与工艺必需字段
   （绣娘拿纹样/绣种，裁缝拿尺寸/剪裁元素），看不到顾客身份、故事全文与报价。
5. **两家门店同改一单不互相覆盖** — 每单互斥锁 + `expected_version` 乐观并发：
   先提交者成功，后提交者收到 409 与当前版本，重读后重试。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `qipao/order.py` | 订单聚合：全部状态迁移、授权、版本指纹、变更单、只增账本、返工、试衣 |
| `qipao/scheduler.py` | 交期预测：工作日历（09:00–17:00）、周末/节假日、技能与绣种系数、DAG 并行、关键路径 |
| `qipao/repository.py` | 线程安全仓储：每单一把锁 + 乐观版本检查 |
| `qipao/app.py` | 应用服务：命令通道（统一 expected_version）与读模型入口 |
| `qipao/projections.py` | 角色投影：工匠最小任务卡、门店视图、公开故事视图、成衣记录 |
| `qipao/catalog.py` | 工艺路线 DAG、角色技能系数、节假日日历、门店/工匠/面料批次注册表 |
| `qipao/http_api.py` | JSON HTTP 适配（无第三方依赖） |
| `qipao/clock.py` | UTC 时钟、时区转换、可复现的测试时钟 |
| `tests/` | 22 个用例：领域规则、两店并发（含 HTTP 409）、成衣记录端到端、排程 |

## 工艺路线

```
咨询 → 设计确认 → 备料 ┬→ 刺绣（苏绣/湘绣）─┐
                      └→ 裁剪 ──────────────┴→ 缝制 → 试衣 → 交付
```

- 咨询/设计/备料/试衣/交付是里程碑，由量体、顾客确认、领料、试衣记录、
  交付等领域动作自动物化为完工工序；刺绣/裁剪/缝制走派工系统。
- 刺绣与裁剪并行；缝制必须等两者都完工。返工生成关联原工序的新 attempt，
  返工未完成时下游工序不能派工。
- 交期预测中工匠工时 = 标准工作日 ÷ 技能效率系数 × 绣种系数，
  按工匠所在地（中国工坊 / 伦敦门店）日历跳过周末与法定节假日。

## 授权模型

- `recording_consent`：顾客授权**记录**故事，可撤回；撤回后不能再确认新制作版本。
- `production_grant`：首次确认版本时成立，绑定该版本，**不可撤回**——
  已确认的制作合同继续履行。
- `publication`（按范围独立授予/撤回，历史追加留痕）：
  - `workshop_internal`：工坊内部可见文化含义（以"工艺提示"形式只下发给在制绣娘）；
  - `public_anonymous`：公开但匿名；
  - `store_marketing`：门店社交平台可署名发布。

  撤回后成衣记录与公开视图立即遮蔽故事正文，但撤回事件与确认人留痕。

## 变更与账本

改稿（`customer_redesign`）、顾客迟到缺席（`late_customer`）、
面料报废（`material_scrap`）都产生**变更单**：

- 变更单提出后，受影响工序及其下游锁定（并行兄弟工序不受影响）；
- 面料报废必须先补录替代批次，顾客才能确认；
- 顾客确认后生成新指纹版本（旧版留档）、追加费用条目、登记延期天数与确认人；
- 迟到改约确认后自动生成新的试衣预约。

账本（费用/工时）只增不改：报价应收、收款、工时工费、变更补费均为追加条目，
返工与改稿永不冲销已收款和已完成工时。

## HTTP API

```
GET  /health
POST /orders                              {customer_id, store_id, actor}
GET  /orders/{id}?actor={person_id}       按角色返回裁剪后的视图（必须带 actor）
GET  /orders/{id}/garment                 成衣记录：版本链/延期归因/确认人/公开故事范围
GET  /orders/{id}/schedule?include_pending=true
POST /orders/{id}/commands/{command}      body 必须携带 actor 与 expected_version
```

命令包括：`add_store`、`record_story`、`set_consent`、`set_publication`、
`add_measurement`、`submit_draft`、`approve_version`、`raise_change`、
`amend_change`、`confirm_change`、`reject_change`、`allocate_material`、
`scrap_material`、`assign_task`、`start_task`、`complete_task`、
`request_rework`、`log_work`、`record_payment`、`schedule_fitting`、
`mark_late`、`record_fitting`、`deliver`。

版本过期返回：

```json
{"error": "conflict",
 "message": "订单已被其他门店更新，请重新读取后再提交",
 "details": {"expected_version": 7, "current_version": 8}}
```

## 验收演练

- `tests/test_concurrency.py`：梅费尔与考文特花园两店顾问基于同一版本
  并发提交，服务层多线程与真实 HTTP 两种方式均验证恰一者成功、一者 409，
  败者携带新版本重读重试后成功；串行编辑两店都成功。
- `tests/test_garment_record.py`：一张订单历经改稿 → 面料报废换批 →
  试衣迟到改约 → 试衣返工 → 交付，成衣记录逐项给出延期原因
  （3+2+1 天 + 返工）、每笔延期的确认人、四版指纹取代链、
  最终面料批次、返工链指向的原工序；交付后撤回传播许可，
  故事正文立即遮蔽而制作证据不变。
- `tests/test_domain_rules.py`：双授权分离、偷换面料/尺寸被拒、
  只增账本、工匠最小信息、并行工序不被误冻结、跨时区确认时刻。

## 运行

```bash
python3 service.py --check          # 基础身份检查
python3 service.py --port 8000      # 启动 HTTP 服务
npm test                            # 22 个领域用例 + 3 个基线契约用例
```
