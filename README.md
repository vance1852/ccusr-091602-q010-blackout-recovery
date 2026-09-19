# 断电后产线恢复裁决

本项目汇集停电前快照、设备启动报告、物料扫描和命令日志，为每个在制单元形成恢复证据包，
面向无人值守恢复给出一体化裁决与执行。正式恢复与演练使用独立命名空间，证据来源保持不变。

`domain_contract.json` 规定裁决、恢复动作和命令去重结果。检查点按设备与工序定义，
任何执行记录都必须具有稳定业务键和前置依赖。

## 架构

| 模块 | 职责 |
| --- | --- |
| `app/evidence.py` | 证据汇聚：四类来源只追加，证据包取各来源最新一条 |
| `app/adjudication.py` | 纯函数裁决规则，结论必须解释最后可信检查点 |
| `app/planner.py` | 恢复计划：按“公用工程 → 输送 → 加工单元”依赖排序，未就绪不得越级 |
| `app/commands.py` | 命令去重网关：同一业务键只生效一次（accepted/duplicate/stale/dependency_blocked） |
| `app/executor.py` | 按依赖顺序执行；崩溃后幂等重放，不多投一次料 |
| `app/service.py` | 编排入口：裁决 → 计划 → 双人确认 → 执行 → 复核，演练隔离与正式推进视图 |
| `app/store.py` | SQLite 持久化，多步写入同事务，正式/演练按 namespace 隔离 |

## 关键语义

- **裁决四分类**：`auto_resume` / `inspection_required` / `scrap` / `insufficient_evidence`。
  缓存完成回执与物料账一致时采信回执检查点；回执与物料账矛盾时回退快照检查点；
  账差不一致转人工检查；中断敏感工序超时或物理损伤判报废；证据缺失判证据不足。
- **依赖闸口**：加工单元依赖输送、输送依赖公用工程；前置动作未完成时后续动作保持
  `blocked`，阻塞链（`dashboard()["blocking_chains"]`）给出根因。
- **命令去重**：业务键稳定生成；同键同载荷为 `duplicate`，同键不同载荷或旧时间戳为
  `stale`，依赖未就绪为 `dependency_blocked`（不占用业务键，可重试）。
- **双人确认**：人工选择与自动建议不一致时，动作需两名不同确认人确认后才可执行。
- **不可删除**：执行过的动作（executing/completed/review_required）不可删除、不可覆盖；
  后续证据只生成复核结论（confirmed / contradicted），矛盾时动作转 `review_required`。
- **演练**：`start_drill(incident)` 复制正式证据到 `drill:<incident>:<n>` 命名空间，
  可反复执行与 `reset_drill`，正式记录不受任何影响。
- **崩溃安全**：命令与效果同事务落账；进程崩溃后 `recover()` 对在途动作幂等重放，
  已落账命令被去重网关识别为 duplicate，不会多投一次料。

## 使用流程

```python
from app import RecoveryService

svc = RecoveryService("plant.db")
svc.register_unit("INC-1", "U1", "utility")
svc.register_unit("INC-1", "V1", "conveyor", requires=["U1"])
svc.register_unit("INC-1", "P1", "processing", requires=["V1"], profile={...})

svc.record_snapshot(...)        # 停电前快照
svc.record_boot_report(...)     # 设备启动报告
svc.record_command_log(...)     # 命令日志（含缓存完成回执）
svc.record_material_scan(...)   # 物料扫描

svc.adjudicate("INC-1")         # 裁决：每个单元的结论 + 最后可信检查点
svc.create_plan("INC-1")        # 生成依赖有序的恢复计划
svc.execute_ready("INC-1")      # 无人值守执行；崩溃后 svc.recover("INC-1") 续跑
svc.dashboard("INC-1")          # 阻塞链 / 物料账差 / 命令去重结果 / 各单元最终处置
```

运行基础检查：

```bash
python3 -m unittest discover -s tests -v
```
