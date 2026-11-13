# 双截止时点回放层

`warehouse_data.replay` 在原始资料工具之上提供按请求时点（point-in-time）的
特征取值，解决两类事故：离线特征把故障后补录的检测结果放进故障前样本；在线
重放总选“最后到库”的更正，导致同请求两个环境取值不同。

## 口径

每个候选带两个时间：`event_time`（事件发生时刻）与 `available_time`（记录对
系统可得的时刻）。请求带两个独立截止：

- 候选必须同时满足 `event_time <= event_cutoff` 和
  `available_time <= knowledge_cutoff`，两个过滤互不替代；
- TTL 只看事件年龄 `age = event_cutoff - event_time`，`age == ttl` 等号可用；
  某事件赢家过期即停止，不允许回退到更老事件并声称新鲜；
- 同 `entity/feature/event_time` 的**已可得**修订，按 `revision` 大、
  `source_priority` 大、`record_id` Unicode 序大决出赢家。
  `knowledge_cutoff` 之后才到库的高修订没有资格压掉当时可得的旧修订，
  “最后到库”本身不产生任何优先权，败者的败因键写入排除链；
- 事件时刻从新到旧扫描。最新有效赢家为 `delete` 墓碑时，按**该特征自己的**
  `delete_policy` 决定：`missing` 直接缺失，`fallback` 继续向更早事件回退；
- 显式 `null` 是命中有记录但缺测：返回 `value=null` 与 `record_id`，不触发
  回退，与 `delete` 语义严格区分；
- 全部时间取自请求与记录本身，不使用处理机当前时间，结果是原始可得性口径。

## 模块

| 模块 | 职责 |
| --- | --- |
| `replay/models.py` | `Candidate`、`FeaturePolicy`、`FeatureRequest`、`ReplayResult` 与校验，显式 null/delete 枚举、missing 原因枚举 |
| `replay/index.py` | `(entity, feature)` 分桶 + 事件时刻/可得时刻双排序的不可变索引，二分截窗 |
| `replay/engine.py` | 正式索引上的回放主流程与解释链 |
| `replay/reference.py` | 不依赖正式索引的小规模枚举参考，仅供测试交叉校验 |
| `replay/store.py` | 版本化仓：不可变快照、事务提交、请求缓存 |
| `replay/material.py` | 冻结 JSON 清单与回放对象之间的适配 |

## 索引与复杂度

第一层字典把请求定位到 `(entity, feature)` 桶；桶内事件时刻升序，
`bisect_right(event_cutoff)` 之后的事件组根本不进入扫描；组内另维护
`available_time` 升序数组，`bisect_right(knowledge_cutoff)` 切出当时已可得
的修订成员。单请求触达的候选数量只与双截止窗口内的记录有关，不随全库增长。
`tests/test_replay.py::IndexScanBoundedTests` 用带断言的包装验证这一点。

## 版本、事务与缓存

- `FeatureStore.commit` 每次产出一个不可变 `warehouse_version`：迟到更正只能
  进新版本，`record_id` 全仓唯一且不可跨版本复用；固定旧版本 + 相同双截止 +
  相同 `policy_version` 的重放结果稳定；
- 提交在同一把锁内完成形状校验、索引构建、快照发布与缓存挂接；构建失败则
  本次提交无任何副作用（`CommitError` 后 head 与版本集合不变）；
- 缓存键为 `(request_id, entity, feature, event_cutoff, knowledge_cutoff,
  policy_version)`，两个截止与策略版本任一不同都是独立条目，
  一个 `event_cutoff` 的结果绝不会借给另一个 `knowledge_cutoff`；
- 缓存按版本隔离，新版本挂载新缓存，不触碰旧版本缓存。

## 解释输出

每个 `ReplayResult` 包含：

- 命中：`status`、`value`（可为显式 null）、`hit_record_id`、`hit_event_time`；
- 赢家排除链 `exclusions`：未过双截止、修订/优先级/record_id 落败、
  TTL 过期、delete 跳过等，附 `detail` 保留冲突信息；
- TTL 判定 `ttl_judgments`：每个被扫描事件赢家的 `ttl/age/fresh`；
- missing 互斥原因：`no_entity`、`no_available_candidate`、`ttl_expired`、
  `delete_policy_missing`、`unknown_feature`。

`to_dict()` 输出可直接 JSON 序列化。

## 快速验证

```sh
python -m unittest discover -s tests -v
```

关键场景均有专门用例：未来到达的旧事件不可见、更正回溯只在可得后生效、
最新事件删除的回退/missing 两种策略、显式 null 不回退、TTL 等号与过期即停、
重复 request_id 独立输出、缓存键隔离、版本冻结，以及 400 组随机数据上
索引引擎与枚举参考的逐字段等价。
