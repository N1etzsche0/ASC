# JVM 批量导出优化验证

2026-10-13，本地将 ASC 的逐类 CFR 调用改为按来源分组调用。同一个 JAR、嵌套 JAR 或 loose-class 包根中的类共享一次 JVM 和依赖上下文；不同来源保持隔离。多文件入口来自 [CFR 官方 Main 源码](https://github.com/leibnitz27/cfr/blob/master/src/org/benf/cfr/reader/Main.java)，并用本机 CFR 0.152 验证。

## 实测收益

基线为优化前的暂存实现快照，候选为当前实现。每项排除一组预热，随后交替执行三个独立进程样本，取中位数。双方使用同一个输入、CFR 0.152、两个固定 CPU 和两个导出工作线程；文件系统缓存预热。

| 输入 | 类定义 | 优化前 | 默认批处理 | 速度提升 | 优化前/后峰值 RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| `slf4j-api.jar` | 24 | 5.374 s | 0.887 s | 6.06 倍 | 180.2 / 232.3 MiB |
| `commons-lang.jar` | 124 | 36.743 s | 4.365 s | 8.42 倍 | 230.2 / 707.9 MiB |

两个输入均为 ecology9 中的公开依赖库，未导出业务类。双方全部输出成功，三个样本的 **24/24、124/124 个源码 SHA-256 全部相同**，类来源集合也相同。没有对 ecology9 的 331,447 个类定义做全量反编译，不能按这两个 JAR 的倍率推算任意应用耗时。

内存是每次运行的进程树 RSS 采样峰值，再取三个样本的中位数，包含 CFR 子进程。每 10 ms 采样可能错过短暂峰值，共享页可能重复计入。批处理缓存更多类的分析状态，速度收益伴随更高内存；512 MiB Java 堆上限不等于单进程 RSS 上限。

同一个 124 类输入使用 `--batch-size 16 --threads 2` 时，预热后三个样本中位数为 **8.024 s / 445.4 MiB**，全部 124 个源码哈希仍与基线相同。相对固定 64 类批次，峰值 RSS 降约 37%，耗时增加；相对旧实现仍快约 4.58 倍。内存受限时可先用此设置，或进一步降低工作线程数；单线程性能没有在本组测量。本文的性能比较均使用固定参数，自动内存策略不改变这些历史测量。

## 实现与参数

```sh
python main.py export application.war -o /path/to/export \
  --threads 2 --batch-size 64 --java-release 8

python main.py export application.war -o /path/to/export \
  --threads 2 --batch-size 64 --java-release 8 --resume
```

- `--batch-size` 默认按可用内存、cgroup 剩余额度和线程数自动选择 1、8、16、32 或 64；可显式指定 1..256，1 使用逐类调用。每次选中类字节码合计最多 32 MiB，较大的来源组自动拆分。
- 每组复用依赖上下文，仍限定 512 个类 / 128 MiB；每个 worker 的 ZIP 缓存最多同时打开八个归档。
- 前缀和数量限制只选择需要导出的定义；上下文依赖不会增加清单导出项。内部类及匿名类仍各自保留来源和输出路径。
- 每个类完成后立即写入清单。缺少输出或共享上下文失败时，对受影响的类进行一次单独尝试，保留其他成功结果。
- `--timeout` 限制每次 CFR 调用。批次超时直接记录该组错误，避免再累计多个逐类超时；可用更小批次或更长超时续跑。
- 批处理策略与批次大小进入配置指纹；升级或更改大小后，续跑会重新生成受管理的输出。任意未管理文件仍不能覆盖。
- 新后端使用普通 Java/CFR CLI，不需要 javac 或新增运行时依赖。javac 只用于测试样本生成。

## JADX 对照的边界

同一轮测量中，JADX 1.5.6 在 24 类 SLF4J 上成功导出，中位数 1.331 s / 238.9 MiB；当前 ASC 0.887 s / 232.3 MiB。这一组中，ASC 约快 1.50 倍。

Commons Lang 中，JADX 将内部类合并为 77 个源码文件，中位数 2.978 s，但每次有两处 `Method not decompiled` 标记，尽管进程退出 0。该时间不能作为同等成功导出的速度结论。基准脚本为这些不完整输出返回 1；ASC 基线与候选的全部 124 个定义均成功且源码哈希相同。

本验证没有重新编译全部输出或证明语义等价，也没有统计显著性结论。源码哈希一致仅说明这组输入的优化输出与基线文本相同。

## 测试与复现

严格回归：130 项通过，包含真实 CFR、多 JAR 来源隔离、WAR 内嵌匿名类、命名内部类、筛选、字节数拆分、错误隔离、批次超时、哈希续跑，以及可用内存、cgroup 层级限额和自动批次续跑。Python 3.14.6 / OpenJDK 25.0.4；Python 3.11/3.12 的 CI 和发布性能门禁仍需按仓库规范执行。

在源码 checkout 中，用支持 JVM 导出的旧版本目录作为基线：

```sh
python tests/benchmark_jvm_export.py /path/to/slf4j-api.jar /path/to/commons-lang.jar \
  --baseline /path/to/previous-checkout \
  --cfr-jar /path/to/cfr-0.152.jar --jadx /path/to/jadx/bin/jadx \
  --samples 3 --batch-size 64 --threads 2 \
  --output artifacts/jvm/export-comparison
```

省略 `--baseline` 可对比当前实现的逐类模式；省略 `--jadx` 可仅比较 ASC。输出目录必须为空。可选 psutil 用于进程树 RSS；缺少它仍可测时间。每次记录参数、输入 SHA-256、退出码、日志、输出错误及匹配的源码哈希数。

本机原始数据位于 `artifacts/jvm/batch-optimization-20261013/measurements/results.json`，优化前快照位于同级 `baseline/`，均由 Git 忽略。CFR JAR SHA-256 为 `f686e8f3ded377d7bc87d216a90e9e9512df4156e75b06c655a16648ae8765b2`。参见 [JVM CLI](JVM.md) 与 [测试规范](../tests/TESTING.md)。

较小批次的原始记录保存在同级 `batch16/results.json`；使用相同输入、CPU、堆上限、线程数和预热/样本规则。所有源码及原始日志只留在本地忽略目录，提交内容包含实现、测试、基准脚本与本文。
