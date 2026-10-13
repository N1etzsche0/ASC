# CFR 常驻执行层与上下文缓存测试

2026-10-13，使用原始公开 Maven JAR，完成全量导出、续跑和源码一致性检查。
本次以 Java host 承接 CFR 批量执行，减少 Python 重复解析依赖和读取 class 的工作。
CLI、JSONL、来源路径、资源分析和 manifest 继续使用现有接口。

## 基于 CFR 源码的实现选择

[CFR 0.152 Main](https://github.com/leibnitz27/cfr/blob/0.152/src/org/benf/cfr/reader/Main.java)
构建 driver 后调用 `analyse()`。
[CfrDriverImpl](https://github.com/leibnitz27/cfr/blob/0.152/src/org/benf/cfr/reader/CfrDriverImpl.java)
为每个目标类重建 `DCCommonState` 和 dumper；其注释说明直接共享分析缓存会导致测试失败。
ASC 在同一 JVM 中连续调用原版 CFR `Main`，保持参数和状态重置方式，复用 JVM、JIT 和静态 JDK 元数据。
使用未修改的 CFR 0.152 JAR，SHA-256：

```text
f686e8f3ded377d7bc87d216a90e9e9512df4156e75b06c655a16648ae8765b2
```

Java host 使用有长度限制的二进制协议；诊断尾部最多 64 KiB，堆耗尽标记在截断前识别。
超时或失败结束并回收当前 host；高堆重试前关闭空闲 host 并排空活跃任务。
host 源码随 Python 包和源码 ZIP 分发，首次实际 worker 请求时编译到临时目录。
Python 上下文缓存包括全导出共享的依赖元数据 LRU（4,096 条 / 保守计费 16 MiB），
以及每个 archive reader 的 class 字节 LRU（1,024 条 / 16 MiB）；仍最多保留八个 ZIP 句柄。
元数据按当前字节 SHA-256 和类名匹配，不共享可变解析对象。打开的 archive 在导出期间需保持不变；
松散 class 每次重新读取。每批 512 类 / 128 MiB 上下文和来源选择语义保持不变。

## 完整公开 JAR

环境：Linux 6.19.14、Intel Xeon w7-2495X、Python 3.14.6、OpenJDK 25.0.4。
全量任务依次运行，CPU 0..7，八个 worker，普通 JVM 堆上限各 2 GiB，
`--cfr-mode worker --no-enum-switch --no-resources --oom-heap-mb 8192`，自动 batch size 均为 64。
每个 CFR JVM 使用 `-XX:ActiveProcessorCount=1`。
原始输入及 SHA-256 与 [前次完整 ASC/JADX 对比](JVM-LARGE-JAR-COMPARISON.md) 相同。

| 输入 | 大小 / class 定义 | 优化前 / 当前导出 | 观察提速 | 优化前 / 当前峰值 RSS |
| --- | ---: | ---: | ---: | ---: |
| Hive `hive-exec:4.0.1` | 79.54 MiB / 38,481 | 479.52 / 208.90 s | 2.30× | 5,590.67 / 12,823.01 MiB |
| AWS `aws-java-sdk-bundle:1.12.780` | 370.54 MiB / 173,906 | 1,200.47 / 420.62 s | 2.85× | 5,128.84 / 7,553.31 MiB |

优化前为 `af31bd7` / `f5274d0` 批次执行层，当前为 Java host 与上下文缓存。
大 JAR 的前后各一次完整运行，当前包含 host 编译/启动，没有排除预热。
共享主机的可用内存和缓存会变化，比值为单次观察，不表示统计显著性。
RSS 为 10 ms 采样的进程树总和，可能重复计算共享页或漏短峰值，不等于 Java heap。
运行后的哈希/源码诊断检查不计入工具耗时。

| 输入 | 普通 worker 启动 / 请求 | 元数据命中 / 未命中 | 高堆重试 | 导出状态 |
| --- | ---: | ---: | ---: | --- |
| Hive | 9 / 666 | 239,479 / 61,328 | 1 | 38,481 成功，exit 0 |
| AWS | 8 / 2,718 | 1,162,799 / 195,035 | 0 | 173,906 成功，exit 0 |

Hive 失败批次按类隔离重试，因此请求多于普通批次数；九次 worker 启动包括失败后的重启。
另有一次独占 8 GiB 的恢复进程，HiveParser 写入同一个托管 manifest，其源码 SHA-256 与前次一致：

```text
9998e0efa74e70404fdab635e6b9e14d7282087f872821034751e4a96c82ba0d
```

| 输入 | 当前续跑 | 峰值 RSS | 复用文件 | Java / 编译器启动 |
| --- | ---: | ---: | ---: | ---: |
| Hive | 18.78 s | 399.48 MiB | 38,481 / 38,481 | 0 |
| AWS | 63.33 s | 1,629.39 MiB | 173,906 / 173,906 | 0 |

导出/续跑均 exit 0，托管文件与 manifest 哈希不匹配为零；前后来源集合一致，无缺失或新增。

## 原始源码哈希与诊断差异

| 输入 | 原始源码哈希完全相同 | 仅增加 worker 栈注释 | 其他源码差异 |
| --- | ---: | ---: | ---: |
| Hive | 38,454 | 27 | 0 |
| AWS | 173,898 | 8 | 0 |

CFR 失败方法的注释记录异常栈，host 增加反射调用和 `CfrWorker.main` 栈帧。
逐文件 diff 确认这些差异只增加该调用链注释，其他源码文本均未改变。
原始注释和哈希保留；测试单独分类，不修改导出的诊断信息。
质量计数保持一致：Hive 27 文件 / 64 失败标记、44 文件 / 61 结构化警告；
AWS 8 文件 / 20 失败标记、6 文件 / 8 结构化警告。
标记不是不同方法数；成功输出仍可能含失败方法。未验证可编译性或运行语义。

此前 JADX 完整 AWS 输入为 301.01 s / 17,824.13 MiB，当前 ASC 为 420.62 s / 7,553.31 MiB。
JADX 约快 1.40 倍，峰值 RSS 约为 ASC 的 2.36 倍。
JADX 默认 ZIP64 不完整读取的 44.80 s 不参加完整输入比较。
Hive JADX 为 151.33 s / 16,878.52 MiB，但 exit 3，源码诊断与 ASC 不同。

## 配对样本与默认策略

原 AWS Bundle 的 S3 包提取样本含 906 个 class，未加入其他包的依赖。
当前两个执行模式均使用上下文缓存，排除一轮预热后交替运行三对新进程：

| 模式 | 中位耗时 | 中位峰值 RSS | 每次普通 JVM 启动 |
| --- | ---: | ---: | ---: |
| process | 5.863 s | 2,063.06 MiB | 15 |
| worker | 5.486 s | 4,022.14 MiB | 8 |

每次 exit 0，906 个来源和原始源码哈希全部相同，无失败/结构化标记。
样本仅观察到 1.07 倍提速、常驻内存更高，不据此外推完整大 JAR 耗时。
默认 `auto` 在任务超过每 worker 两批、且可用内存保留 25% + 512 MiB 后能覆盖配置堆总量时尝试复用。
小任务、内存未知/不足或编译器不可用时使用 process；显式 worker 要求 JDK。
此策略不动态限制 RSS。Java 执行层适合 CFR 批次调用，收益同时来自减少启动和重复 Python 工作；
后续迁移 archive/class 数据路径到 Java 或 Rust 时，仍需验证来源覆盖、重复定义隔离和完整输出。

## 复现与回归

```sh
python tests/benchmark_jvm_large.py /path/to/original.jar \
  --cfr-jar /path/to/cfr-0.152.jar --threads 8 --heap-mb 2048 \
  --oom-heap-mb 8192 --no-enum-switch --cfr-mode worker \
  --output artifacts/jvm/worker-large

python tests/benchmark_jvm_worker.py /path/to/sample.jar \
  --cfr-jar /path/to/cfr-0.152.jar --threads 8 --heap-mb 2048 \
  --batch-size 64 --no-enum-switch --output artifacts/jvm/worker-pairs
```

Python 3.11.16 / 3.12.14 各通过 155 严格测试、无跳过，六项 Android 性能检查通过。
这是本地验证，独立于 GitHub Actions。源码 ZIP、Python wheel 均包含 Java host 源码。
原始证据位于忽略目录 `artifacts/jvm/cfr-worker-20261013/`：两份完整 `results.json`、
`final-probe-pairs/results.json`、两份 `*-source-consistency.json`、`validation/`、源码/manifest/日志与打包预览。
中途停止的诊断任务标明不完整，未作为性能结果使用；生成文件不进入 Git。
