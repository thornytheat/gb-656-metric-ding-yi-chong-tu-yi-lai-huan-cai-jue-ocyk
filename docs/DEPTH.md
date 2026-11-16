# 深度登记 metricgraph

- 规模：中型（8 个功能模块，95 个文件，108 条测试）
- 核心规则：同名同版本指标先做表达式稳定签名，再按依赖图检测环并裁决生效版本；别名修订先演练再发布。
- 使用者与工作流：分析治理组在指标发布前复核定义冲突、缺依赖、依赖环与版本重叠，别名纠正走「演练 → 发布裁决版本」。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 元数据采信 | 采信 DDL 或 JSON 元数据，看逐行诊断与隔离区 | POST /api/schemas/import | `app/modules/m1_intake/handler.py` `app/modules/m1_intake/service.py` `app/modules/m1_intake/reader.py` `app/modules/m1_intake/ddl.py` `app/modules/m1_intake/sheet.py` | `tests/test_m1_intake.py` |
| M2 类型格 | 查类型格的值域与刻度，或直接比两个类型写法 | GET /api/types | `app/modules/m2_types/handler.py` `app/modules/m2_types/service.py` `app/modules/m2_types/coerce.py` `app/modules/m2_types/range.py` `app/modules/m2_types/lattice.py` | `tests/test_m2_types.py` |
| M3 冲突判定 | 对同名指标做签名并列出定义冲突 | POST /api/metrics/conflicts | `app/modules/m3_compat/handler.py` `app/modules/m3_compat/metric_rules.py` `app/modules/m3_compat/priority.py` | `tests/test_m3_compat.py` `tests/test_metric_rules.py` |
| M4 字段映射 | 看旧名到新名的映射、别名 SQL、冲突项 | GET /api/mapping | `app/modules/m4_mapping/handler.py` `app/modules/m4_mapping/service.py` `app/modules/m4_mapping/mapping.py` `app/modules/m4_mapping/sqlalias.py` `app/modules/m4_mapping/rename.py` | `tests/test_m4_mapping.py` |
| M5 影响面 | 看受影响的下游节点、环、断链、单边登记 | GET /api/impact | `app/modules/m5_impact/handler.py` `app/modules/m5_impact/service.py` `app/modules/m5_impact/closure.py` `app/modules/m5_impact/cycle.py` `app/modules/m5_impact/broken.py` | `tests/test_m5_impact.py` |
| M6 版本批次 | 跑批次、看历史、验陈旧拒绝与区间重叠 | POST /api/runs | `app/modules/m6_batches/handler.py` `app/modules/m6_batches/service.py` `app/modules/m6_batches/window.py` `app/modules/m6_batches/guard.py` `app/modules/m6_batches/history.py` | `tests/test_m6_batches.py` |
| M7 指标目录 | 导出一份定义、依赖与状态清单 | GET /api/export/catalog.csv | `app/modules/m7_migration/handler.py` `app/modules/m7_migration/service.py` `app/modules/m7_migration/csv_out.py` `app/modules/m7_migration/ledger.py` | `tests/test_m7_migration.py` |
| M8 裁决版本 | 别名修订先演练，看受影响指标、冲突、环与采用依据，再发布新裁决并按版本导出目录 | POST /api/rulings/dry-run · POST /api/rulings/publish · GET /rulings | `app/modules/m8_rulings/handler.py` `app/modules/m8_rulings/service.py` `app/modules/m8_rulings/alias.py` `app/modules/m8_rulings/metrics.py` `app/modules/m8_rulings/csv_out.py` | `tests/test_m8_rulings.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 导出件带字节序标记，首列名被污染 | 首列名带 `\ufeff`，按名取值全空 | 探测编码前先剥标记，再按回退链解码 | `app/modules/m1_intake/reader.py` | `tests/test_m1_intake.py::test_trap02_bom_stripped_before_decode` |
| TRAP-05 | 列定义行尾多一个分隔符 | 拆出空列，或者把坏行当正常列 | 顶层分隔符计数，命中即整行进隔离区 | `app/modules/m1_intake/ddl.py` | `tests/test_m1_intake.py::test_trap05_extra_separator_quarantined` |
| TRAP-09 | 字段身份是 2^53+1 这种大整数 | JSON 往返落进 float，末位被改 | 身份一律字符串保形，浮点身份直接拒绝 | `app/modules/m1_intake/sheet.py` | `tests/test_m1_intake.py::test_trap09_big_integer_field_id_kept_as_string` |
| TRAP-17 | 上游改列名、放宽类型 | 按列名对账，改名算删除加新增 | 只按 field_id 对齐，放宽另算兼容 | `app/modules/m3_compat/judge.py` | `tests/test_m3_compat.py::test_trap17_rename_is_not_delete_plus_add` |
| TRAP-19 | 一个字段同时改名、换类型、改必填 | 结论取决于规则书写顺序 | 显式优先级表，重排输入结论不变 | `app/modules/m3_compat/priority.py` | `tests/test_m3_compat.py::test_trap19_rule_order_does_not_change_primary` |
| TRAP-20 | 下游依赖成环、节点指向自己 | 递归爆栈或静默截断 | 三色 DFS，自引用单列一类 | `app/modules/m5_impact/cycle.py` | `tests/test_m5_impact.py::test_trap20_cycle_and_self_loop` |
| TRAP-21 | 上下游只登记一边 | 只查单向，另一半关系漏报 | 双向核验，缺边清单逐条列出 | `app/modules/m5_impact/broken.py` | `tests/test_m5_impact.py::test_trap21_one_sided_registration` |
| TRAP-22 | 同一字段被连续改过两次名 | 只认最近一跳，最早的名字解析不到 | 跨版本改名链一路解析到底 | `app/modules/m4_mapping/rename.py` | `tests/test_m4_mapping.py::test_trap22_alias_chain_resolves_multi_hop` |
| TRAP-25 | 两个版本生效区间重叠 | 取到哪版看运气 | 半开区间比较，重叠直接标记警告 | `app/modules/m6_batches/window.py` | `tests/test_m6_batches.py::test_trap25_overlap_flagged_but_adjacent_clean` |
| TRAP-26 | 基于旧版本号提交 | 后提交无条件覆盖新结果 | 版本号乐观锁，基线落后即 409 | `app/modules/m6_batches/guard.py` | `tests/test_m6_batches.py::test_trap26_stale_submission_rejected` |
| TRAP-27 | 别名表纠正了一条误映射 | 直接改映射表，旧裁决的同名分组与签名跟着变，历史结论被改写 | 每次修订是整表快照，裁决版本钉住当时的别名，读旧版只认旧表 | `app/modules/m8_rulings/alias.py` `app/modules/m8_rulings/service.py` | `tests/test_m8_rulings.py::test_old_ruling_keeps_its_alias_snapshot` |
| TRAP-28 | 同一别名指向两个不同目标 | 后写覆盖先写，或随便挑一个合并 | 判歧义，进待处理清单，不猜测合并 | `app/modules/m8_rulings/alias.py` | `tests/test_m8_rulings.py::test_ambiguous_alias_is_pending_not_merged` |
| TRAP-29 | 发布写库途中失败 | 序号被占、半截写入，目录读不出来 | 单事务写入，失败整笔回滚，序号不占，上一版照常可读 | `app/modules/m8_rulings/service.py` `app/store.py` | `tests/test_m8_rulings.py::test_insert_ruling_failure_leaves_no_row_and_no_seq_gap` |
| TRAP-30 | 演练之后别名规则又更新了 | 拿旧演练直接发布，目录里混进过期含义 | 规则指针随每次提交前移，旧演练发布被拒 `ALIAS_REVISION_STALE` | `app/modules/m8_rulings/service.py` | `tests/test_m8_rulings.py::test_old_dry_run_rejected_after_alias_rule_update` |
| TRAP-31 | 同一份内容重复点发布 | 每次都新增一版裁决，序号膨胀 | 发布键去重，同一内容只返回已发布版本 | `app/modules/m8_rulings/service.py` `app/store.py` | `tests/test_m8_rulings.py::test_publish_creates_version_and_is_idempotent` |

## 对账规则

- 字段账：旧版本 = 未变 + 改名 + 换类型 + 改必填 + 冲突 + 删除
- 字段账：新版本 = 未变 + 改名 + 换类型 + 改必填 + 冲突 + 新增
- 引用账：登记引用 = 可达 + 断链，其中可达 = 直接 + 经别名
- 采信账：列定义行 = 接受字段 + 阻断行
- 别名账：提交别名 = 生效映射 + 待处理（歧义、成环、多目标）
- 页面上四账并列显示，差额不为 0 即判不平

## 已知不覆盖

- SQL 只认 `别名.列名`；无别名的裸列名、子查询、CTE、函数包裹的列都不抽。
- DDL 只解析 `CREATE TABLE` 的列定义，不处理分区、约束、索引与 `ALTER`。
- 类型格只覆盖数值、字符、时间、布尔四族；映射表、数组、结构体一律算 `unknown`，保守判破坏。
- 生效区间重叠只标记不阻断，最终以版本序号为准。
- 打印页脚用 `counter(page)`，Chrome 打印预览不渲染该计数。
- 指标表达式的别名替换仍是整词替换再切 `+` 交换项：`-`、`*`、`/`、括号内交换与聚合函数参数不理解交换律。
- 别名歧义只挂起待处理，不提供自动消解；发布本身不因歧义阻断，只把歧义项排除在有效映射外。
- 依赖环检测看的是指标正名的图，环只报告不阻断发布。
