# ecology9 本地兼容性验证

验证日期：2026-10-13。输入来自
`/home/kali/Projects/AnsibleProjects/code-audit-ansible/projects/ecology9`。
该目录只读使用；工具实现和输出位于 ASC 工作区，没有连接 VM 或运行应用代码。

## 输入特点

- `target-src-vm108` 包含 35,715 个物理 class、975 个 JAR，存在 Java 8/6/5 及历史 45.3 字节码。
- 多个应用根和依赖归档包含同名类，部分定义不同；批量导出必须按来源隔离。
- `target-src-20260813` 包含 97 个 JSP 和 21 个 XML，其中部署描述符包含 Resin、Jersey 和 XFire 元数据。
- 部分 XML 是截取片段；`app/ecology/WEB-INF/web.xml` 带孤立闭合标签。
- `target-src-vm108` 没有标准 `org/apache/jsp` / `*_jsp.class` 生成目录。编译 JSP 的 SMAP 支持使用合成样本验证，不能将该语料中的普通 Servlet 类当成可恢复的 JSP。

## 已执行验证

| 验证 | 结果 |
| --- | --- |
| 完整 `target-src-vm108` 清单，Java release 8 | 331,447 个类定义，300,948 个类名，27,054 组同名类，5,647 个资源，清单错误 0 |
| 本机完整清单耗时 | 22.57 秒；包含物理 class 名称解析、归档目录及嵌套归档处理，不代表全部反编译耗时 |
| `target-src-20260813` 静态源分析 | 覆盖 97 个 JSP，633 条指令，82 条 JSP 动作，701 个 Java 代码块；70 个源反射语法位置，其中 47 个字面量、23 个动态值 |
| XML 框架元数据 | 15 个可解析 XML：14 个文档、1 个明确标记的片段；包含 15 条 XFire 注册 |
| 完整 `target-src-vm108/WEB-INF/web.xml` | 67 个组件、96 个映射、1 条框架扫描配置、2 条 JSP 配置，错误 0 |
| `classbean/com/api/demo` 编译元数据样本 | 检出 1 条框架注解，错误 0 |
| 5 个小型接口的真实 CFR 批量导出 | 全部成功；随后续跑全部跳过已验证输出 |

六个截取的规则 XML 没有完整受支持的框架块，结果保留 `analysis-error`，总体源分析命令返回 1。已有 JSP/框架记录仍保留。该缺口没有被当成完整解析或反编译失败。

批量反编译样本为：

- `classbean/weaver/hrm/mapper/HrmMapper.class`
- `classbean/weaver/general/mapper/InitMapper.class`
- `classbean/com/weaver/formmodel/ui/define/IWebUIContent.class`
- `classbean/com/weaver/formmodel/ui/define/IWebUIScript.class`
- `classbean/weaver/social/worker/IWorker.class`

这些小型接口用于验证 Java 6/8 输入、来源布局、真实 CFR 和恢复机制。没有对全部 331,447 个定义进行批量反编译，也没有声称所有业务类反编译成功。

## 复现入口

以下命令在 ASC 项目目录运行。使用独立输出目录；现有输出需要 `--resume`。

```sh
export ECOLOGY9_CORPUS=/home/kali/Projects/AnsibleProjects/code-audit-ansible/projects/ecology9
export ASC_CFR_JAR=/home/kali/.cache/asc-backends/cfr-0.152.jar
mkdir -p artifacts/ecology9

python main.py analyze "$ECOLOGY9_CORPUS/target-src-20260813" \
  --sources-only --json -o artifacts/ecology9/source-analysis.jsonl

python main.py analyze "$ECOLOGY9_CORPUS/target-src-vm108/WEB-INF/web.xml" \
  --kind framework --sources-only --json -o artifacts/ecology9/web-analysis.jsonl

python main.py analyze "$ECOLOGY9_CORPUS/target-src-vm108/classbean/com/api/demo" \
  --limit 200 --json -o artifacts/ecology9/compiled-analysis.jsonl

python main.py export "$ECOLOGY9_CORPUS/target-src-vm108/classbean/weaver/hrm/mapper/HrmMapper.class" \
  --classpath "$ECOLOGY9_CORPUS/target-src-vm108/classbean/weaver/general/mapper/InitMapper.class" \
  --classpath "$ECOLOGY9_CORPUS/target-src-vm108/classbean/com/weaver/formmodel/ui/define/IWebUIContent.class" \
  --classpath "$ECOLOGY9_CORPUS/target-src-vm108/classbean/com/weaver/formmodel/ui/define/IWebUIScript.class" \
  --classpath "$ECOLOGY9_CORPUS/target-src-vm108/classbean/weaver/social/worker/IWorker.class" \
  --no-resources --threads 2 --resume -o artifacts/ecology9/interface-export
```

本地输出保存在 `artifacts/ecology9/`，由 Git 忽略；框架和反射报告仅包含结构元数据，不复制配置值。报告中的定义关联与字面量目标属于静态证据，不等同于运行时装载、实际可达路径或安全结论。
