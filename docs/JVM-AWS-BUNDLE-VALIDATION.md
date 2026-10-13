# 超大型公开 JAR：AWS SDK Bundle

2026-10-13 使用 Maven Central 发布的
[AWS Java SDK Bundle 1.12.780](https://repo.maven.apache.org/maven2/com/amazonaws/aws-java-sdk-bundle/1.12.780/aws-java-sdk-bundle-1.12.780.jar)
验证类清单、完整源码导出、续跑及源码质量诊断。导出与续跑的实现提交为
`ea05ff1`；类清单在优化前的 `ef353ba` 测量，其清单读取路径保持不变。
该输入约为上一轮 Hive JAR 文件大小的 4.66 倍，所选定义数约为 4.52 倍。

## 输入与参数

| 项目 | 实测值 |
| --- | --- |
| Maven 坐标 | `com.amazonaws:aws-java-sdk-bundle:1.12.780` |
| 文件大小 | 388,543,471 bytes / 370.54 MiB |
| ZIP 条目 | 178,095 |
| 原始 `.class` 条目 | 173,907 |
| 所选类定义 | 173,906 |
| class 解压后总大小 | 707,150,167 bytes |
| 平台 | Linux 6.19.14 x86_64，Intel Xeon w7-2495X，94.29 GiB 主机内存 |
| 解释器 / JVM / 后端 | Python 3.14.6 / OpenJDK 25.0.4 / CFR 0.152 |
| 导出设置 | `--threads 8 --heap-mb 2048 --no-enum-switch --no-resources` |
| batch-size | 启动时自动选择 64；可用内存采样约 21.52 GiB |
| Java release / 后端超时 | 默认 17 / 每次 CFR 调用默认 120 秒 |

下载内容与发布方 `.jar.sha1` 一致，并独立计算了 SHA-256：

```text
JAR SHA-1:   308a3af95a47e0c4e1f8bd98a37657d4661ae45e
JAR SHA-256: 31db25d93f0082f421d219a033968fbafc0587f9d62f652dac5d017598861aab
CFR SHA-256: f686e8f3ded377d7bc87d216a90e9e9512df4156e75b06c655a16648ae8765b2
```

该 JAR 没有声明 `Multi-Release: true`。其
`META-INF/versions/9/module-info.class` 不进入所选定义，因此覆盖率的
分母是 173,906。测试未执行 AWS SDK 或连接 AWS 服务，资源复制已关闭。

## 测量方法与结果

每项为一个新进程的实测结果，没有按小样本外推完整耗时。类清单、全量
导出和续跑均固定使用 CPU 0..7，每个 CFR JVM 设置
`-XX:ActiveProcessorCount=1`。基准采集每 10 ms 汇总 Python 和子进程 RSS；
这可能遗漏短时峰值或重复计入共享页，不能当作堆使用量。
导出计时包含进程启动、输入读取、依赖暂存、CFR 和输出写入；基准脚本
随后执行的源码质量扫描和结果核查不计入导出命令的墙钟时间。
文件系统缓存状态没有作为冷缓存条件控制，没有统计显著性结论。

| 操作 | 时间 | 采样峰值 RSS | 退出码 | 结果 |
| --- | ---: | ---: | ---: | --- |
| `listclass --json` | 2.659 s | 231.98 MiB | 0 | 173,906 条定义记录 |
| 完整 `export` | 1,217.22 s | 5,212.05 MiB | 0 | 173,906 个源码文件，零类错误 |
| 原目录 `export --resume` | 83.38 s | 1,628.44 MiB | 0 | 173,906 个成功文件全部复用 |

两次导出命令均没有超时，最终 manifest 全部为 `ok`，源文件 SHA-256
与 manifest 的核对均为零不匹配。续跑没有启动 CFR 重试。
该成功状态表示源文件生成和完整性检查通过；以下方法级诊断仍需单独考虑。

| 源码质量诊断 | 完整导出 / 续跑 |
| --- | ---: |
| Java 文件 | 173,906 |
| 包含失败标记的文件 | 8 |
| `This method has failed to decompile.` 标记 | 10 |
| `Decompilation failed` 标记 | 10 |
| 包含 `Unable to fully structure code` 的文件 | 6 |
| `Unable to fully structure code` 标记 | 8 |

两类失败标记可能属于同一方法，20 次文本命中不能解释为 20 个不同失败
方法。源码没有经过全量重新编译或语义验证。本次 stderr 还包含依赖暂存
达到 512 类 / 128 MiB 上限的警告；CFR 使用裁剪后的上下文继续处理，
因此不能假定所有依赖类型均已完整解析。关闭 enum-switch 还原的输出
保留了编译器生成的映射表达式。

## 大样本暴露的目录开销与改进

优化前每个导出批次及其依赖暂存各自建立 ZIP 读取上下文。对这个包含
178,095 条目录项的 JAR，单次打开目录的三次局部探测为
0.503 / 0.565 / 0.648 秒。批次较小时，重复目录解析会占用较多 Python CPU。

实现改为每个工作线程在一次导出期间保留独立归档读取器，所选 class、
依赖暂存及单类重试共用该线程的读取器。每个读取器仍最多缓存八个归档，
使用 LRU 淘汰；执行器结束后关闭全部缓存，批次失败和提前关闭生成器也
会释放句柄。线程之间不共享 ZIP 句柄。该优化不改变所选字节码或后端参数，
所以不修改已有续跑配置指纹。

优化前的完整导出尝试在 371.36 秒时中断，保留了 12,416 个成功输出，
当时没有已记录错误。它没有完成全量，不能作为完整导出基线或用于计算
全量速度提升比例。新运行与这 12,416 个共同输出的 SHA-256 全部一致。
缓存会在导出期间保留归档目录元数据，仍应结合 worker 数观察实际 RSS。

## 复现与证据

在项目根目录安装依赖、配置本地 CFR，并安装可选的 `psutil` 以采样 RSS：

```sh
mkdir -p artifacts/jvm/public-aws
curl --fail --location \
  https://repo.maven.apache.org/maven2/com/amazonaws/aws-java-sdk-bundle/1.12.780/aws-java-sdk-bundle-1.12.780.jar \
  --output artifacts/jvm/public-aws/aws-java-sdk-bundle-1.12.780.jar
sha256sum artifacts/jvm/public-aws/aws-java-sdk-bundle-1.12.780.jar
export ASC_CFR_JAR=/absolute/path/to/cfr-0.152.jar
python main.py listclass artifacts/jvm/public-aws/aws-java-sdk-bundle-1.12.780.jar --json
python tests/benchmark_jvm_large.py \
  artifacts/jvm/public-aws/aws-java-sdk-bundle-1.12.780.jar \
  --cfr-jar "$ASC_CFR_JAR" --threads 8 --heap-mb 2048 --no-enum-switch \
  --run-timeout 7200 --output artifacts/jvm/public-aws/measured-run
```

基准输出目录必须为空。脚本在首次导出目录内续跑，核对 manifest 中的
成功状态、输出 SHA-256 和复用数量，并另外报告 CFR 方法失败标记与
结构化警告。退出码非零或出现失败标记时，应查看具体来源和日志；成功
生成文件不保证源码完整、可编译或语义等价。

本地原始证据位于 `artifacts/jvm/aws-bundle-20261013/`：`input.json`、
`inventory.json`、`zip-open-diagnostic.json`、中断试验日志、
`reader-cache-full/results.json`、导出/续跑 stdout 与 stderr、manifest、
`reader-cache-source-comparison.json` 及回归/性能门禁报告。
JAR、生成源码和原始产物由 `artifacts/` 规则忽略，未提交至仓库。

读取器复用的回归覆盖正常完成、批次超时、提前关闭生成器、线程句柄
隔离及真实 CFR 输出。Python 3.11.16 和 3.12.14 严格回归各 137 项通过、
零跳过。大型 JAR 的 Python 3.14.6 环境是独立诊断环境，没有新增 CI
支持版本。另见 [测试约定](../tests/TESTING.md)、
[JVM 实现结构](JVM-ARCHITECTURE.md)和 [README 结果](../README.md#validation-and-scope)。
