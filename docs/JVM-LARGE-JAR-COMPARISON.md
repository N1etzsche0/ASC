# 大型公开 JAR：ASC / JADX 对比与堆不足恢复

本页保留常驻 worker 改造前的完整测量。当前执行层、上下文缓存、前后提速、
内存取舍和原始源码哈希差异见 [CFR worker 测试](JVM-CFR-WORKER-PERFORMANCE.md)。

2026-10-13 使用原始 Maven Central JAR，实测完整源码导出和 ASC 续跑。
ASC 的 Hive 运行对应 `af31bd7`；AWS 运行对应 `f5274d0`，两者 JVM
运行时代码相同，后者补充 JADX ZIP64 诊断适配器与测试。
JADX 使用未修改的官方 1.5.6 分发包，CFR 使用 0.152。
原先 Hive 的一个堆不足错误已在受管理的全量导出中恢复，完整导出和
续跑均返回 0。旧失败试验保留在 [Hive 历史记录](JVM-LARGE-JAR-VALIDATION.md)。

## 输入与测量约定

| 公开 Maven 坐标 | 原始大小 | ASC 所选定义 | SHA-256 |
| --- | ---: | ---: | --- |
| `org.apache.hive:hive-exec:4.0.1` | 83,402,986 bytes / 79.54 MiB | 38,481 | `dd5e22aed78a7e62d8351e314fb438d90219f5c10014391448d30878c3ed3b03` |
| `com.amazonaws:aws-java-sdk-bundle:1.12.780` | 388,543,471 bytes / 370.54 MiB | 173,906 | `31db25d93f0082f421d219a033968fbafc0587f9d62f652dac5d017598861aab` |

下载来源、发布方 SHA-1 及清单测量见 [Hive](JVM-LARGE-JAR-VALIDATION.md)
和 [AWS](JVM-AWS-BUNDLE-VALIDATION.md) 原始验证记录。两个 JAR 均没有
`Multi-Release: true`；所选定义排除版本目录中的八个 / 一个 class。
没有执行应用类、连接应用服务或复制应用资源。

平台为 Linux 6.19.14 x86_64、Intel Xeon w7-2495X，主机物理内存
94.29 GiB。基准解释器为 Python 3.14.6，JVM 为 OpenJDK 25.0.4。
Python 3.14 是独立诊断环境；项目回归矩阵仍为 3.11 / 3.12。

两工具顺序运行，固定 CPU 0..7 和八个工作线程。ASC 每个 CFR JVM
设置 `-XX:ActiveProcessorCount=1`，JADX 单 JVM 设置为 8。
ASC 使用 `--heap-mb 2048 --oom-heap-mb 8192 --no-enum-switch
--no-resources`，自动 batch-size 为 64；每次后端调用超时为默认 120 秒。
JADX 使用 `-Xmx16384m --config none --no-res -j 8` 和默认代码变换。
双方最多同时配置 16 GiB Java 堆；大堆重试期间其他 CFR JVM 已退出。
原生 JVM、Python、目录缓存和文件映射另占内存，所以 RSS 可以超过堆上限。

每项为一个新进程的单次实测，没有单独预热，也没有统计显著性结论。
文件系统缓存未作为冷缓存控制；主机并非专用空闲测量环境。
回归与输入清单探测使用 CPU 22 / 23，Android 门禁使用 CPU 23。
墙钟时间包括工具启动、读取、反编译和写入，不含随后基准脚本的质量扫描。
RSS 每 10 ms 汇总进程树，可能重复计入共享页或遗漏短时峰值。

```text
CFR JAR SHA-256: f686e8f3ded377d7bc87d216a90e9e9512df4156e75b06c655a16648ae8765b2
JADX all.jar SHA-256: fe3e12c45acf75f92369685fd02d1d7a7323385dc725680a9b98a0dac0ea554b
JADX adapter SHA-256: 4bec3ab08fddb95136e9c45cdb126e0570fcad934e8e5c510d8777bd5b4a515b
```

## 导出与续跑

| 输入 | 工具 / 操作 | 墙钟时间 | 采样峰值 RSS | Java 文件 | 退出码 |
| --- | --- | ---: | ---: | ---: | ---: |
| Hive | ASC 完整导出 | 479.52 s | 5,590.67 MiB | 38,481 | 0 |
| Hive | ASC 原目录续跑 | 18.95 s | 374.00 MiB | 全部 38,481 复用 | 0 |
| Hive | JADX 默认 CLI | 151.33 s | 16,878.52 MiB | 14,641 | 3 |
| AWS Bundle | ASC 完整导出 | 1,200.47 s | 5,128.84 MiB | 173,906 | 0 |
| AWS Bundle | ASC 原目录续跑 | 83.43 s | 1,628.48 MiB | 全部 173,906 复用 | 0 |
| AWS Bundle | JADX 默认 CLI，输入不完整 | 44.80 s | 16,675.92 MiB | 58,339 | 0 |
| AWS Bundle | JADX JDK ZIP API 适配器，完整读取 | 301.01 s | 17,824.13 MiB | 154,944 | 0 |

ASC AWS 本轮全量导出 173,906 项均为 `ok`，manifest / 文件哈希没有
不匹配，堆不足重试数为 0。随后续跑复用全部 173,906 项，exit 0，
哈希零不匹配，无 CFR 重试；本轮全部源码 SHA-256 与 `ea05ff1` 运行一致。

ASC 为每个所选定义单独生成文件，保留 origin 并核对 manifest SHA-256；
JADX 会合并内部类、内联匿名类和重新命名。因此 Java 文件数不等于
同一口径的类覆盖率，也不适合直接做文件哈希相等比较。
JDK 适配器逐项核对全部 173,906 个 AWS 输入 class origin，缺失和额外均为 0。
这是输入覆盖检查，不能证明每个方法都被完整还原。
独立的默认读取路径清单探测确认 Hive 加载 38,481 项、零缺失；AWS
仅加载 64,307 项，缺失 109,599 项，零额外。这些清单探测不进入导出计时。

## JADX 的 ZIP64 输入差异

AWS JAR 有 178,095 个 ZIP 条目，使用 ZIP64。原始默认 CLI 虽然返回 0，
但直接调用同一分发包 ZIP 读取器只得到前 65,535 项、64,307 个 base class。
源码路径核查还发现 96,660 个顶层 class 路径没有对应输出。
这不能仅用内部类合并解释，也不能将 44.80 秒用作完整导出的速度基准。

JADX 1.5.6 的
[JadxZipParser](https://github.com/skylot/jadx/blob/v1.5.6/jadx-commons/jadx-zip/src/main/java/jadx/zip/parser/JadxZipParser.java)
从传统 EOCD 读取 16 位条目数，ZIP64 检测检查磁盘字段。
该样本磁盘字段为 0、条目数为 `0xffff`，因此未切换到支持 ZIP64 的读取路径。
这一解释由原始 EOCD、实际读取计数及官方源码共同支持。

`tests/JadxLargeJar.java` 使用 JADX 已有的 `FALLBACK_AS_DEFAULT`，
强制其 JDK ZIP 读取器，同时保留路径/压缩比检查，设置条目上限 200,000。
它匹配 CLI 的缓存、代码写入器、插件发现和环境配置，然后记录加载
origin，再通过 JADX API 导出。编译适配器不计入墙钟时间，JVM 启动和
输入载入计入。没有改写公共 JAR 或 JADX 二进制。
这是明确标注的 API 适配器测试，不冒充默认 CLI 的行为。

## Hive 堆不足修复

旧配置在 `org.apache.hadoop.hive.ql.parse.HiveParser` 的 SSA 分析中
耗尽 2 GiB 堆，完整导出和续跑均返回 1。此前独立 `getclass` 补导虽成功，
没有修复原 manifest。

本次增加 `export --oom-heap-mb 8192`，只对独立调用确认的 Java heap
space / GC overhead limit exceeded 再尝试一次。重试先等待普通 CFR
进程退出，阻止新调用，并串行处理大堆任务；不会在八个普通 JVM 之外
额外叠加一个 8 GiB JVM。其他错误、超时或第二次失败仍写入错误并返回 1。
选项默认为关闭，不在用户未配置时自动增加堆。

全量运行的 `oom_retries` 为 1，HiveParser 记录 `status: ok`、
`decompiler_heap_mb: 8192`，源码 SHA-256 为：

```text
9998e0efa74e70404fdab635e6b9e14d7282087f872821034751e4a96c82ba0d
```

旧运行中 38,480 个成功输出与本次全部同哈希；新生成的 parser 与先前
精确单类 8 GiB 输出同哈希。随后续跑复用全部 38,481 项，当前运行重试数为 0。
两次 manifest / 文件哈希核对均没有不匹配，恢复结果进入了同一受管理目录。
重试堆参与配置指纹，改变它会重新生成受管理输出；续跑应保持相同选项。

## 源码诊断

| 输入 / 后端 | 失败标记文件 / 命中数 | CFR 结构化警告文件 / 命中数 | JADX warning 文件 / 命中数 | JADX error 文件 / 命中数 |
| --- | ---: | ---: | ---: | ---: |
| Hive / ASC | 27 / 64 | 44 / 61 | 0 / 0 | 0 / 0 |
| Hive / JADX 默认 CLI | 55 / 86 | 不适用 | 1,249 / 6,170 | 5 / 6 |
| AWS / ASC | 8 / 20 | 6 / 8 | 0 / 0 | 0 / 0 |
| AWS / JADX 默认 CLI，输入不完整 | 0 / 0 | 不适用 | 254 / 5,788 | 0 / 0 |
| AWS / JADX JDK ZIP 适配器 | 8 / 8 | 不适用 | 1,546 / 17,332 | 0 / 0 |

扫描分别统计 `Method not decompiled`、`Could not decompile`、
`Decompilation failed`、`This method has failed to decompile.`，
以及 CFR 结构化说明和 `JADX WARN:` / `JADX ERROR:`。
Hive CFR 的 64 次失败标记来自 32 条失败注释和 32 个 stub，可能对应
相同方法；不能解释为 64 个不同失败方法。
JADX warning 包含重命名冲突等情况，不能全部解释为方法反编译失败。
不同后端的诊断格式不相同，不能仅比较命中数评判还原准确性。
源码没有全量重新编译或语义验证；退出码 0 和完整输入覆盖也不代表零失败方法。

## 复现与本地证据

从上述 Maven 坐标下载并核对 SHA-256，安装本地 CFR 和官方 JADX 1.5.6。
安装可选 `psutil` 以采样 RSS。各基准输出目录应为空，两工具依次运行。

```sh
python tests/benchmark_jvm_large.py /path/to/public.jar \
  --cfr-jar /path/to/cfr-0.152.jar --threads 8 --heap-mb 2048 \
  --oom-heap-mb 8192 --no-enum-switch --run-timeout 3600 \
  --output artifacts/jvm/comparison/asc
python tests/benchmark_jvm_large_compare.py /path/to/public.jar --tool jadx \
  --jadx /path/to/jadx-1.5.6/bin/jadx --threads 8 --jadx-heap-mb 16384 \
  --zip-max-entries 200000 --output artifacts/jvm/comparison/jadx
python tests/benchmark_jvm_large_compare.py /path/to/public.jar --tool jadx \
  --jadx /path/to/jadx-1.5.6/bin/jadx --jadx-jdk-zip \
  --threads 8 --jadx-heap-mb 16384 --zip-max-entries 200000 \
  --output artifacts/jvm/comparison/jadx-jdk
```

最后一个命令用于 ZIP64 AWS 完整输入补测。报告记录 API 适配器和
JADX JAR 的 SHA-256、加载 origin 集合、进程退出码和文本诊断。
进程失败、超时或已核查的 origin 缺失均导致基准脚本非零退出；方法失败
标记单独报告，不改写工具的实际退出码。

原始证据保留在 `artifacts/jvm/large-comparison-20261013/`：
`hive-asc/`、`hive-jadx/`、`aws-asc/`、`aws-jadx/`、`aws-jadx-jdk/` 的
结果、stdout/stderr、受管理源码和 manifest；另外保留
`zip-reader-diagnostic.json`、`zip-probe/` 的源码/加载 origin 日志、
`hive-source-consistency.json`、`aws-source-consistency.json`、
`jadx-source-quality-reviewed.json` 和
`default-jadx-input-coverage.json`，以及
`gates/` 的回归/Android 门禁报告。生成输入、源码与原始运行产物不进入 Git。

Python 3.11.16 / 3.12.14 严格回归各 142 项通过，零跳过，覆盖真实 CFR、
堆失败识别、单次重试、并发排斥、失败释放、配置指纹和续跑计数。
31 对交替新进程的 Android 比较、九次启动和九次 supplied DEX 门禁均通过。
详见 [测试约定](../tests/TESTING.md) 和 [README](../README.md#validation-and-scope)。
