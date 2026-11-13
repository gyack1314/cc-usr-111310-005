# 预测特征仓的双截止时点回放

此项目保存生产与供应链协同的湖仓原始资料。`warehouse_data.io`读取JSON清单及核对文件身份，`warehouse_data.domain`检查基本形状并输出数量，`warehouse_data.cli`展示摘要。[资料约定](docs/data-contract.md)定义字段、类型、时间、数量和版本范围。设备、批次、组织、事件和成本均为合成，不连接真实工厂或生产仓库。

Python3.11以上，无第三方Python依赖。项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q warehouse_data tests
python -m warehouse_data.cli inspect data/normal.json
```

`inspect`只读原始资料，不生成仓库新版本或计算领域结果。`invalid-config.json`有基本形状错误；其余清单有完整资料身份，但可能包含需要业务操作判断的冲突、缺口或不满足条件的边界。null与缺字段分别保留，有理数和decimal用字符串存储，不能先经float丢精度。

## 双截止时点回放层

`warehouse_data.replay`按请求携带的`event_cutoff`与`knowledge_cutoff`做时点取值：候选必须同时满足`event_time<=event_cutoff`和`available_time<=knowledge_cutoff`，同事件时刻的已可得修订按revision、source_priority、record_id决胜，再按事件时刻从新到旧选择；TTL按`event_cutoff-event_time`判断且等号可用，delete墓碑按各特征自己的策略missing或回退，显式null命中不回退。正式实现走entity/feature分桶加双时间二分索引，批请求不全库扫描；`replay.reference`提供不依赖索引的小规模枚举参考。版本不可变、迟到更正只进新版本，索引更新、版本发布与请求缓存同事务，缓存键含两个截止与策略版本。设计与解释输出见[回放层说明](docs/replay-layer.md)，口径见[资料约定](docs/data-contract.md)。

Dockerfile固定Linux镜像，`docker build -t warehouse-material .`构建；`docker run --rm warehouse-material`显示摘要；`docker run --rm warehouse-material python -m unittest discover -s tests -v`运行已有检查。所有资料本地可读，不需要账号、外部数据库或在线数据源。
