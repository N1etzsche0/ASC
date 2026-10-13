# 大型公开 JAR 验证

2026-10-13 对实现提交 `5f5ee68` 进行本地测试。样本是 Maven Central
公开发布的 [Apache Hive hive-exec 4.0.1](https://repo.maven.apache.org/maven2/org/apache/hive/hive-exec/4.0.1/hive-exec-4.0.1.jar)。
本文记录实际全量导出、续跑和单类补导，不根据小样本推算全量速度。
后续更大输入及读取器复用的验证见
[AWS SDK Bundle 测试](JVM-AWS-BUNDLE-VALIDATION.md)。本页保留原 Hive 测量值。
后续 `af31bd7` 已通过受管理的完整导出恢复这一堆不足错误，38,481 项
全部成功，续跑全部复用，均为 exit 0。当前结果和真实 JADX 对比见
[大型 JAR 对比与修复记录](JVM-LARGE-JAR-COMPARISON.md)；下文是旧配置历史。

## 输入与环境

| 项目 | 实测值 |
| --- | --- |
| Maven 坐标 | `org.apache.hive:hive-exec:4.0.1` |
| 文件大小 | 83,402,986 bytes / 79.54 MiB |
| ZIP 条目 | 40,559 |
| 原始 `.class` 条目 | 38,489 |
| 所选类定义 | 38,481 |
| 所有 class 条目解压后总大小 | 190,755,725 bytes |
| 操作系统 | Linux 6.19.14, x86_64 |
| CPU | Intel Xeon w7-2495X；进程可用 24 个逻辑 CPU |
| 主机物理内存 | 94.29 GiB；全量导出启动时可用内存约 22.43 GiB |
| 解释器 / JVM / 后端 | Python 3.14.6 / OpenJDK 25.0.4 / CFR 0.152 |
| 全量参数 | `--threads 8 --heap-mb 2048 --no-enum-switch --no-resources` |
| 自动 batch-size | 64；没有传入固定 `--batch-size` |
| Java release / 超时 | 默认 17 / 每次 CFR 调用默认 120 秒 |

该 JAR 没有声明 `Multi-Release: true`，因此其八个 `META-INF/versions/`
class 条目不进入所选定义。此处的覆盖率分母为 38,481，而不是所有 ZIP
class 条目的数量。导出不执行被分析应用，资源复制已关闭。

下载后核对了发布方 `.jar.sha1`，并独立计算 SHA-256：

```text
JAR SHA-1:   45986232e934bf332de997bcb2f448b840b6ce5e
JAR SHA-256: dd5e22aed78a7e62d8351e314fb438d90219f5c10014391448d30878c3ed3b03
CFR SHA-256: f686e8f3ded377d7bc87d216a90e9e9512df4156e75b06c655a16648ae8765b2
```

## 全量结果与失败恢复

每项为一次独立进程运行的墙钟时间。全量与续跑固定使用 CPU 0..7，每个
CFR JVM 设置 `-XX:ActiveProcessorCount=1`；单类补导固定使用 CPU 22。
RSS 为每 10 ms 采样的 Python 和子进程 RSS 总和，可能漏掉短时峰值或重复
计入共享页，不能当作 Java 堆使用量。测试包含启动、读写和后端工作，未将
文件系统缓存状态作为冷启动条件，也没有统计显著性结论。

| 操作 | 时间 | 采样峰值 RSS | 退出码 | 结果 |
| --- | ---: | ---: | ---: | --- |
| `listclass --json` | 0.598 s | 62.80 MiB | 0 | 38,481 条类记录 |
| 全量 `export` | 601.49 s | 5,872.12 MiB | 1 | 38,480 `ok`，1 `error` |
| 原目录 `export --resume` | 106.94 s | 2,645.62 MiB | 1 | 复用 38,480 项，重试的 1 项仍失败 |
| `getclass HiveParser`，单进程 8 GiB 堆、180 秒超时 | 43.41 s | 4,226.04 MiB | 0 | 独立产生剩余类的源码 |

全量运行及续跑均没有达到整个运行的超时上限。38,480 个成功文件的
SHA-256 均与 manifest 一致，续跑复用了全部成功结果。
续跑时间包含对失败类的重新反编译，不能理解为纯哈希校验耗时。

唯一失败类为 `org.apache.hadoop.hive.ql.parse.HiveParser`。CFR 在 2 GiB
堆下报告 `OutOfMemoryError: Java heap space`，栈涉及 SSA 分析中的
`SSAIdentifiers` 和 `HashMap`。以 `getclass` 精确选择该类、使用 8 GiB
堆后成功输出 2,298,154 bytes 源码，SHA-256 为：

```text
9998e0efa74e70404fdab635e6b9e14d7282087f872821034751e4a96c82ba0d
```

全量导出与独立补导合计覆盖 38,481 个所选定义。补导文件位于独立目录，
没有改写原导出的错误记录；全量命令的退出码仍为 1。这不是一次全量
8 GiB 参数的成功运行，也不能将两次计时相加作为统一配置的全量基准。
`--prefix` 是前缀过滤，`--limit 1` 不保证选择外层类；精确单类补导应使用
`getclass`。

## 源码质量诊断

文件产生、哈希校验成功与方法完整还原分别检查：

| 诊断 | 38,480 个批量输出 | 单独补导的 HiveParser |
| --- | ---: | ---: |
| 包含失败标记的文件 | 27 | 0 |
| `This method has failed to decompile.` 标记 | 32 | 0 |
| `Decompilation failed` 标记 | 32 | 0 |
| 包含 `Unable to fully structure code` 的文件 | 43 | 1 |
| `Unable to fully structure code` 标记 | 60 | 1 |

失败注释和失败 stub 可能出现在同一方法；64 次失败标记命中不能解释成
64 个不同的失败方法。结构化警告也不等同于整个类没有源码。标记扫描是
文本诊断，未验证全部输出的编译性或语义等价性；关闭 enum-switch 还原后，
源码保留编译器生成的映射表达式。

## 中断试验与调参依据

保留了两个未完成试验的原始日志。它们没有完成全量工作，不能与上表计算
速度提升比例：

| 配置 | 中断前运行时间 | 当时已记录成功 / 错误 | 已观察问题 |
| --- | ---: | ---: | --- |
| 8 workers，512 MiB/进程，默认 enum-switch 还原 | 896.54 s | 10,914 / 103 | 已记录错误均为堆不足 |
| 8 workers，2 GiB/进程，默认 enum-switch 还原 | 512.34 s | 10,688 / 832 | 已记录错误为批次超时 |

生成的 Thrift enum-switch 类存在分析成本较高的情况。局部探测中，
`ThriftHiveMetastore$1` 提升到 2 GiB 后成功输出；
`ThriftHiveMetastore$add_foreign_key_result` 在 512 MiB 下关闭 enum-switch
还原后成功输出。最终全量测量采用 2 GiB 和 `--no-enum-switch`。
这些观察支持将堆大小和还原选项作为显式参数，不足以证明所有堆不足或
超时都具有同一根因。CFR 的 [enum-switch 说明](https://www.benf.org/other/cfr/switch-on-enum.html)
描述了相关编译器映射结构。

默认堆仍为 512 MiB，默认保留 enum-switch 还原；大输入可根据实测调参。
堆限制按 worker 计费，自动 batch-size 并不保证复杂类的内存上限。
提高 `--heap-mb` 或改变 `--no-enum-switch` 会改变续跑配置指纹，不能直接
以不同参数续跑全目录并预期复用原有全部结果。

## 复现

在项目根目录安装依赖并配置本地 CFR。用于 RSS 采样的 `psutil` 是基准
脚本的可选依赖；缺少它时不会得到可比较的内存数据。

```sh
mkdir -p artifacts/jvm/public-hive
curl --fail --location \
  https://repo.maven.apache.org/maven2/org/apache/hive/hive-exec/4.0.1/hive-exec-4.0.1.jar \
  --output artifacts/jvm/public-hive/hive-exec-4.0.1.jar
sha256sum artifacts/jvm/public-hive/hive-exec-4.0.1.jar
export ASC_CFR_JAR=/absolute/path/to/cfr-0.152.jar
python main.py listclass artifacts/jvm/public-hive/hive-exec-4.0.1.jar --json
python tests/benchmark_jvm_large.py \
  artifacts/jvm/public-hive/hive-exec-4.0.1.jar \
  --cfr-jar "$ASC_CFR_JAR" --threads 8 --heap-mb 2048 --no-enum-switch \
  --run-timeout 1800 --output artifacts/jvm/public-hive/measured-run
```

基准输出目录必须为空。脚本完成全量导出后在同一 managed 目录续跑，
保存命令、JSONL 进度、stderr、结果 JSON、源码与哈希检查结果。有类失败
时基准脚本返回非零；先检查报告，再精确补导：

```sh
mkdir -p artifacts/jvm/public-hive/recovery
JAVA_TOOL_OPTIONS=-XX:ActiveProcessorCount=1 python main.py getclass \
  artifacts/jvm/public-hive/hive-exec-4.0.1.jar \
  org.apache.hadoop.hive.ql.parse.HiveParser \
  --heap-mb 8192 --no-enum-switch --timeout 180 \
  -o artifacts/jvm/public-hive/recovery/HiveParser.java
```

本地证据位于 `artifacts/jvm/large-public-jars-20261013/`，包括 `input.json`、
`inventory.json`、`no-enum-full/results.json`、各运行原始日志、manifest、
`hiveparser-heap8192/result.json` 和 `final-recovery-validation.json`。
JAR、反编译源码及原始运行产物由 `artifacts/` 忽略规则排除，未提交至仓库。
共享的结果为本文、README 和可重复运行的基准/诊断脚本。

同一实现提交的 Python 3.11.16 / 3.12.14 严格回归各有 135 项通过、零跳过，
Android 配对性能、启动和 supplied DEX 门禁均通过。大型 JAR 使用的
Python 3.14.6 是单独诊断环境，未作为新增 CI 支持版本。详见
[测试约定](../tests/TESTING.md)和 [README 测试结果](../README.md#validation-and-scope)。
