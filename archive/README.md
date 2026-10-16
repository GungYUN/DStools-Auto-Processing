# 开发与测试资料存储区 / Development archive

本目录保存项目的设计、实现记录、历史程序、测试脚本、样本和验收证据。
日常运行请使用[当前说明文档](../README.md)，本目录不随日常部署上传。

| 位置 | 内容 |
| --- | --- |
| `GENERALIZATION_PLAN.md`、`SINGLE_SOURCE_PLAN.md` | 批量通用化及单源程序的设计方案 |
| `IMPLEMENTATION_STATUS.md` | 开发过程与实现状态记录 |
| `PROJECT_README.md` | 本次整理前的项目入口 |
| `docs/` | 旧操作说明、阶段测试指令、参数对照及验收记录 |
| `tests/` | 新版回归、现场检查脚本及比较/接受记录 |
| `examples/validation/` | 测试表格与参考源信息 |
| `legacy/` | 从 UCD 版复制的历史程序、文档及回归测试 |

已有验收结论保留：Stage1、Stage2、Stage3A/3B 和 Stage4B 已通过；Stage4A 的
数值差异已按用户决定接受；独立 Stage4C 现场验证本轮跳过，并非现场 PASS。
详细证据见[验证记录](docs/VALIDATION.md)和[验收范围](docs/STAGE4_PLAN_zh.md)。

历史命令保留了当时的站点路径、目录和时间背景。文中的 `tests/...`、
`examples/validation/...` 和 `legacy/...` 对应本目录下的文件；
不能将旧指令不加调整地作为当前部署操作。
为公开发布，本目录内的个人路径、账户和邮箱已替换为占位值。比较记录保留原数值和
验收结论，并明确标记为路径已替换的公开副本；接受记录同时保存原证据和公开副本的
SHA。未经替换的原始文件另存于 Git 忽略的本地存储区，不随公开版本发布。
测试基线中的源码 SHA 对应替换路径前的原文件，不代表公开副本的文件指纹。

如需本地回归，在项目根目录执行：

~~~bash
python -m unittest discover -s archive/tests -v
(cd archive/legacy/ozstar_askap && python -m unittest discover -s tests -v)
~~~

第一条需要项目依赖，第二条历史回归还需要 pandas。现场检查脚本需要相应站点、
容器和原验收工作区，不属于日常使用步骤。

2026-10-15归档后复跑：新版141项及历史66项回归均通过。文档链接、使用指令解析、
shell语法及模拟部署清单已核对；原UCD项目、新版处理核心、配置和时长默认值的
文件指纹与整理前一致。日常部署清单仅移除了测试目录，不再上传归档资料。

随后为公开发布替换了个人路径和账户，并将宿主 Python 默认值改为启动命令的解释器。
新增一项批量/单源解释器选择与显式覆盖的回归后，新版142项和历史66项均通过。
科学选项、资源默认值与时长估算规则保持不变；原始证据在本地私有目录保留。

This directory retains design notes, previous guides, regression code, site
acceptance scripts, fixtures and historical implementation. Use the
[current guides](../README.md) for normal work. The archive is excluded from
deployment. Historical site commands describe the layout used at the time;
archived test programs use the current repository layout.

Personal paths and account names in public archive copies have been replaced
with placeholders. Numerical evidence and conclusions are preserved; redacted
JSON records are labelled and linked with their published hash. Unredacted
originals are retained locally and excluded from Git.

最新 query 修正包含7项有符号 CASDA 赤经回归；当前149项测试全部通过。
本次只区分私人环境与公用模板，未改变 query 修正或时长估算规则。

## 2026-10-16 配置与运行方式更新

当前批量和单源均直接读取 `config.py`，不再使用 site JSON 或要求复制配置。
每次启动采用当前设置；旧运行记录保留用于追溯，未提交的计划可以直接替换，
已提交的旧作业结束或取消后也可以重新规划。指令与有效参数保存在工作目录的
`logs/commands/`，被替换的源表输入保存在 `logs/catalogues/`。
本目录中较早文档的独立 site 设置、冻结计划和禁止修改输入等说明属于历史设计，
当前使用方式以根目录及 `docs/` 为准。验收脚本已适配统一配置和可替换计划。
新增16项配置更新、Slurm衔接、日志、重试和成功结果保留检查；当前回归共165项。
Stage1 的19项离线检查也已适配输入修正与历史保留。科学参数及估时默认值未改动。
