# 跨文化旗袍定制流转

连接伦敦两家门店、设计工作室与苏州/长沙刺绣制作人员的定制业务后端。
覆盖从社交平台咨询到成衣交付的全过程：顾客故事与传播许可、多稿版设计、
顾客确认后冻结不可替换的制作版本、按技能派工与最小信息披露、量体复测、
材料批次、工序返工、跨时区试衣确认，以及受技能/并行工序/节假日影响的交期预测。

## 运行

```bash
python3 service.py --check          # 身份与领域装配检查
python3 service.py --port 8000      # 启动 HTTP 服务（内存事件存储 + 样例目录）
npm test                            # 旧契约 + 24 项领域/验收测试
```

仅使用 Python 标准库（含 `zoneinfo`），无第三方依赖。

## 领域流程

```
咨询 inquiry → 设计确认 design → 备料 material → 刺绣 embroidery
            → 裁剪 cutting → 缝制 sewing → 试衣 fitting → 交付 delivered
```

- **稿版只增不改**：设计师可提交 r1、r2…，同一 revision 不可覆盖；
  顾客确认关键元素、量体版本与报价后，对这三项计算 SHA-256 `manifest`
  冻结为制作版本；历史可经 `GET /orders/{id}/manifest` 重算哈希核查，防止偷偷替换。
- **故事传播许可与制作授权分离**：许可范围为门店展示 / 社交平台 / 营销物料，
  顾客可随时撤回；撤回只影响对外可见的故事正文，不影响已确认合同、收款与生产。
- **收款与工时只追加**：事件存储只追加（`domain/events.py`），无任何改写/删除命令；
  改稿、迟到、材料报废产生的附加费只在新冻结版本上叠加，旧收款与旧工时原样保留。
- **变更单**：改稿 / 量体复测 / 材料报废 / 试衣迟到都会开出待确认变更单，
  冻结受影响阶段的工序推进；顾客再确认后给出新费用与新交期，并按
  `redo_from`（material/embroidery/cutting/…）决定从哪道工序重做，之前工序沿用。
- **返工**：质检退回把原工序标记 rejected，新派一条 `parent_task_id` 指向原工序的
  返工任务；原工序在返工完成后才算收口，阶段方可推进。
- **最小信息**：绣娘/裁缝接口只返回本人任务及该工序 scope 内字段
  （苏绣/湘绣：图案元素与绣区；裁剪/缝制：尺寸与纸样元素），
  不暴露顾客故事、报价、收款与他人任务。
- **交期预测**（`domain/scheduler.py`）：备料→刺绣组（苏绣/湘绣/钉珠并行，
  同技能多任务在在册工匠间贪心排队）→裁剪→缝制→门店试衣缓冲；
  工期按工坊日历（中国节假日）与门店日历（英国 bank holiday）顺延，
  结果列出撞期节假日、单人技能风险与待确认变更的最早新承诺日期。
- **跨时区**：事件统一存 UTC，订单按顾客时区（如 Asia/Shanghai / Europe/London）
  展示本地确认时刻。

## 两店并发控制

每个订单是一条事件流，写命令带 `X-Expected-Version`（GET 订单返回的 `version`）。
两家门店基于同一版本同时修改时：一方 200 成功，另一方收到
`409 version_conflict`（含期望/实际版本），刷新后重试即可。
`tests/test_acceptance.py::test_two_stores_concurrent_edit_one_wins_one_retries`
通过真实 HTTP 双线程演练该场景。

## HTTP 接口

所有命令请求带头 `X-Actor: <人员id>`，乐观并发带 `X-Expected-Version: <n>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查（旧契约） |
| GET | `/directory` | 门店、人员、角色、工艺、节假日目录 |
| POST | `/orders` | 建单（body 可含 `order_id`） |
| GET | `/orders` | 跨店订单看板 |
| GET | `/orders/{id}` | 订单视图（绣娘/裁缝自动最小化） |
| POST | `/orders/{id}/commands/{cmd}` | 执行命令（见下） |
| GET | `/orders/{id}/predict` | 交期预测 |
| GET | `/orders/{id}/manifest` | 冻结版本哈希核查 |
| GET | `/orders/{id}/garment` | 成衣记录（交付后） |

命令：`add_note` `record_story` `grant_consent` `withdraw_consent`
`submit_design` `take_measurement` `give_quote` `confirm` `record_payment`
`prepare_material` `scrap_material` `assign_task` `start_task` `log_work`
`complete_task` `reject_task` `advance_stage`
`request_design_change` `reconfirm` `decline_change`
`schedule_fitting` `mark_fitting_done` `mark_fitting_late` `deliver`。

角色与权限见 `domain/app.py` 的 `ROLE_PERMISSIONS`；
样例账号见 `domain/samples.py`（如 `adv-soho`、`adv-shore`、`des-zhao`、
`mgr-qin`、`art-su-1`、`art-xiang-1`、`cust-lin`）。

## 成衣记录

交付后 `GET /orders/{id}/garment` 给出：承诺/实际日期、`delay_reasons`
（来自历次再确认变更单，如材料色差、试衣迟到）、`confirmer_chain`
（每次冻结版本的确认人与交期）、`total_paid`/`final_price`、累计工时，
以及故事的 `public_scopes`/`withdrawn_scopes`——即从成衣本身即可说明
延期原因、确认人以及获准公开的故事范围。

## 代码结构

```
domain/
  errors.py       领域错误 → HTTP 状态码
  timeutil.py     UTC 存储与 IANA 时区换算
  events.py       只追加事件存储 + 乐观并发
  directory.py    角色/门店/工艺/工作日历目录
  samples.py      首批样例（两门店、苏绣湘绣人员、中英节假日）
  order.py        订单聚合：fold 状态机 + 全部命令守卫
  scheduler.py    技能池/并行工序/节假日/返工/变更交期预测
  projections.py  门店视图、绣娘最小视图、成衣记录、看板
  repository.py   加载—折叠—决策—乐观提交
  app.py          鉴权与命令分发
  httpapi.py      JSON/HTTP 边界
tests/            领域测试 + HTTP 验收测试
service.py        入口与装配
```

当前为内存事件存储；事件结构已是落库形态，后续可把 `EventStore`
替换为持久化实现而不影响领域层。
