# 断电后产线恢复裁决

面向无人值守恢复的一体化裁决系统。整厂短时断电后，现场状态彼此矛盾
（MES 认为工序仍在执行、控制器重启回到待机、完成回执缓存未上传），
直接按任一系统续跑都可能重复投料或遗漏检验。本系统把仓库里的
**停电前快照、设备启动报告、物料扫描、命令日志**汇成每个在制单元的
证据包，给出可解释、可演练、崩溃安全的恢复裁决与执行。

## 能力总览

- **证据包**：四类证据按单元汇聚，自动标注跨源矛盾（MES/控制器不一致、
  回执未上传、物料账差）。
- **裁决**：四分类 `auto_resume` / `inspection_required` / `scrap` /
  `insufficient_evidence`；每条结论都解释所采用的**最后可信检查点**
  （检查点按设备与工序定义，启动报告故障会压低可信度）。
- **计划**：按依赖层级执行 —— 公用工程 → 输送 → 加工，上游未就绪时
  下游动作保持 `blocked`，不得越级；阻塞链可查询。
- **命令去重**：业务键唯一；`accepted` / `duplicate` / `stale` /
  `dependency_blocked`。停电前的旧命令在网络恢复后到达不得重复生效；
  被依赖阻塞的命令不消耗业务键，就绪后重发可生效。
- **双人确认**：人工选择与自动建议不一致时，须由与发起人不同的第二人
  确认后才生效。
- **不可变性**：执行过的恢复动作不可删除；事后到达的证据只生成
  **复核结论**（不一致时动作置为 `review_required`，原始执行记录保留）。
- **演练**：`create_drill()` 复制证据到独立命名空间，可针对同一次停电
  反复演练，正式记录不受污染。
- **崩溃安全**：执行按 intent → effect → outcome 三阶段写前日志，
  效果经命令网关按业务键幂等下发；进程崩溃后 `resume_execution()`
  重放，不会多投一次料。
- **正式推进视图**：阻塞链、物料账差、命令去重结果、各单元最终处置。

`domain_contract.json` 规定裁决、恢复动作和命令去重结果，代码枚举与之
对齐（见 `tests/test_contract.py`）。

## 快速开始

```python
from app import RecoveryService

svc = RecoveryService("recovery.db")
svc.open_outage("OUT-1", occurred_at="2026-09-19T02:00:00")

# 台账与检查点路线
svc.register_unit("UT-POWER", "utility")
svc.register_unit("LINE-1", "conveying", utility_id="UT-POWER")
svc.define_route("DEV-1", "OP-1",
                 ["enqueue", "feed", "process", "inspect", "complete"])
svc.register_unit("U-1", "processing", device_id="DEV-1",
                  line_id="LINE-1", utility_id="UT-POWER", recipe_qty=5.0)

# 证据入库
svc.ingest_snapshot("U-1", "DEV-1", "OP-1", "process", "in_progress", 5.0,
                    recorded_at="2026-09-19T01:55:00")
svc.ingest_boot_report("DEV-1", "standby",
                       restarted_at="2026-09-19T02:05:00")
svc.ingest_material_scan("U-1", 5.0, scanned_at="2026-09-19T02:05:00")

# 裁决 -> 计划 -> 执行
decisions = svc.adjudicate()          # 含最后可信检查点解释
svc.build_plan()
svc.execute_ready()                   # 崩溃后用 svc.resume_execution() 续跑

# 正式推进视图：阻塞链 / 物料账差 / 去重结果 / 最终处置
report = svc.progression_report()

# 演练（不污染正式记录）
drill = svc.create_drill()
svc.adjudicate(ns=drill)
svc.build_plan(ns=drill)
svc.execute_ready(ns=drill)
```

## 架构

```
app/
  models.py        领域模型、契约枚举、异常
  store.py         SQLite 持久层：命名空间隔离、显式事务、审计事件
  evidence.py      四类证据入库与证据包组装（跨源矛盾标注）
  adjudication.py  最后可信检查点计算 + 有序裁决规则
  planning.py      依赖层级计划、阻塞链、状态推进
  commands.py      命令网关：业务键去重、旧命令判死、依赖阻塞
  execution.py     三阶段写前日志执行器，崩溃重放幂等
  approvals.py     人工改判与双人确认
  review.py        事后证据复核（只生成复核结论）
  reporting.py     正式推进视图
  service.py       RecoveryService 门面
```

持久化采用 SQLite（WAL）。所有业务表带 `ns` 列：`official` 为正式
记录，`drill-<id>` 为演练副本；演练仅复制证据类表，不含任何执行记录。

## 运行检查

```bash
python3 -m unittest discover -s tests -v
```
