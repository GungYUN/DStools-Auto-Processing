# 第三阶段：单数据集真实处理与共享处理

Stage2已由用户在Ozstar完成：16项PASS、0项未覆盖。通过的运行目录为
`/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage2/run-sxr6_93m`。
四个真实查询有106条目标—观测关联、57个独立数据集、25个共享组。
Stage3仅选其中一个数据集，不执行Stage2的26/28数据集计划。

当前进度：用户已提供3A单源和3B三源共享的 `STAGE3 PASS`，报告分别位于下文
single/shared3工作区的`state/stage3-check.json`。共享预处理/建模各一次，SIF一致。
无需再重跑两个案例；后续见[Stage4安排](STAGE4_PLAN_zh.md)。
该PASS覆盖流程与文件协议，未代替人工DS/FITS科学内容检查。

## 1. 两个顺序执行的案例

| 案例 | 目标 | 数据集 | 验收重点 |
| --- | --- | --- | --- |
| 3A single | Anchor A | SB74372 beam28 | 真实下载、完整处理、原始输入/模型/完整FITS与独立cutout保留 |
| 3B shared3 | Anchor A、Partner+02、Partner-03 | 同一数据集 | 一次预处理/建模、三份目标独立处理和DS/I/V、完成后清理与版本发布 |

精确key为
`1a0d2f532f040b634ebd43363ca54169062bf27d5e6d36e186e67f04a0eb3fe1`。
CASDA归档估计616591360字节（约588MiB）；它不是所有MS副本和模型的占用总量。
SIF SHA256为
`95a579cfe4e43ac48e19831270b6d81ad678f0ed5e601fd37106706d46aa7316`。

每个案例都使用新的独立工作区。prepare只复制元数据并生成计划；download才
进行CASDA登录/下载；run必须加submit才提交实际Slurm任务。先验收3A，再开始3B。
准备阶段逐个核对源坐标、缓存指纹及SIF与Stage2一致，不复制Stage2数据库/jobs。

本轮资源沿用旧Gate8小规模验收：1槽×8线程、40GiB总内存、10GiB节点临时盘。
估算为2小时/数据集、0.5小时/目标、2小时固定开销，故单源请求4.5小时、
共享三源请求5.5小时；这是假设估时，不是已测真实耗时。48小时只是允许上限，
20分钟退出余量沿用核心默认。使用正常sbatch；交互式2小时限制不适用于此案例。

科学参数保持正式流程，不通过降低像素数、迭代数或通道数改变验收范围。
worker首次自动对实际SIF执行五个工具的help检查，保存sif-help.json。
无需另做完整DStools算法验证。

## 2. 自行与参考历元

Stage2表没有自行，仅验证J2000参考坐标上的查询与计划。本阶段使用
examples/validation/stage3_sources.json中从正式版目录复制的pmra_formula/pmdec_formula：

| 源 | pmRA含cosδ，mas/yr | pmDec，mas/yr | 参考历元 |
| --- | --- | --- | --- |
| Anchor A（UCS689） | -1 | -5 | J2000 |
| Partner+02（UCS690） | 20 | 34 | J2000 |
| Partner-03（UCS693） | 44 | -4 | J2000 |

这些是目录参考值，不是人工PM测试值。查询仍用原参考位置；科学处理按真实
CASDA/MS观测时间修正坐标。新版本采用Astropy历元与cosδ约定，旧版本的线性
修正算法不同；新旧DS或cutout不能一律要求逐字节相同。先比较流程/参数，再
解释坐标差异。单源与共享中的Anchor使用一致设置，适合进行本轮产物比较。

## 3. 本地更新上传

```bash
cd /PATH/TO/DStools_Auto_Processing
./deploy.sh YOUR_OZSTAR_USER@ozstar.swin.edu.au /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
```

## 4. Ozstar准备单源案例3A

```bash
cd /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
module load gcc/13.3.0 python/3.12.3 apptainer
ASKAP_HOST_PYTHON=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/env/askap-python/bin/python
ASKAP_ACCEPTED_SIF=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/dstools/dstools-260925.sif
ASKAP_STAGE2_RUN=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage2/run-sxr6_93m
ASKAP_STAGE3_WORK=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage3/single_SB74372_beam28
set -o pipefail

"$ASKAP_HOST_PYTHON" -u tests/validate_processing_stage3.py prepare \
  --case single \
  --stage2-run "$ASKAP_STAGE2_RUN" \
  --work-dir "$ASKAP_STAGE3_WORK" \
  --container "$ASKAP_ACCEPTED_SIF" \
  --python "$ASKAP_HOST_PYTHON" \
  --casda-username YOUR_CASDA_USERNAME \
  --password-file "$HOME/.config/askap/casda-password" \
  2>&1 | tee stage3_single_prepare.log
```

用户名和密码文件沿用正式版默认；如你的实际值不同，请在准备前替换参数。
密码只由download/run在运行时读取，不写入快照或上传。已有保护文件可直接用，
文件权限须为600；也支持原有CASDA_PASSWORD环境覆盖，不需要在聊天发送密码。

prepare预期打印STAGE3 PREPARED: single、恰好1个dataset/1个target、8CPU及4.5h。
生成stage3-case.json、single_site.json、查询快照、冻结计划与sbatch，尚未下载或提交。
已有非空目录会拒绝再次prepare；后续用download/run/check恢复，避免改写证据。

## 5. 下载与计算

先进行真实CASDA登录和下载，完成后打印STAGE3 DOWNLOADED，仍不提交Slurm：

```bash
"$ASKAP_HOST_PYTHON" -u tests/validate_processing_stage3.py download \
  --work-dir "$ASKAP_STAGE3_WORK" \
  2>&1 | tee stage3_single_download.log
```

下载检查归档、checksum存在且身份一致。持久工作区需容纳原始MS、处理副本、
残差MS、完整模型和插模副本；节点tmp不是这些持久数据的存储位置。
可用quota -s检查组空间/文件额度，不以df整个文件系统剩余空间代替配额。

download通过后提交实际任务，并保持登录controller运行：

```bash
"$ASKAP_HOST_PYTHON" -u tests/validate_processing_stage3.py run --submit \
  --work-dir "$ASKAP_STAGE3_WORK" \
  2>&1 | tee stage3_single_run.log
```

该命令负责提交、等待和续跑，结束时自动执行完成检查。在站点允许的持久会话
中运行较方便；掉线后重新定义变量并执行同一run命令恢复，不重新prepare。
不要在此阶段更新正在运行的代码。日志显示Job是pipeline job标识，Slurm数字ID
在state/jobs/*.submission.json；sacct的COMPLETED还需结合下面的产物/状态检查。

## 6. 复核结果

独立再次检查（只读科学文件和SQLite，仅保存检查报告）：

```bash
"$ASKAP_HOST_PYTHON" -u tests/validate_processing_stage3.py check \
  --work-dir "$ASKAP_STAGE3_WORK" \
  2>&1 | tee stage3_single_check.log
```

结果STAGE3 PASS表示本案例的实际链、状态、产品和保留协议通过。
STAGE3 NOT_READY表示controller/worker尚未结束；STAGE3 FAIL会列出失败步骤及日志。
报告保存为state/stage3-check.json。自动检查至少包括：

- 精确数据集/目标、冻结SIF、成功指针、各科学步骤及实际时间/坐标记录一致。
- DS/I/V都存在并记录SHA256，不只依赖controller退出码或Slurm状态。
- 单源tar、原始MS、input.ms、input.subtracted.ms、完整I/V和模型、插模副本保留。
- 原始MS payload逐文件与tar比SHA；内部硬链接按目标普通文件核对内容及同inode关系。
  table.lock作为casacore临时锁单独列出排除。
- 插模命令作用于insertion_model，cutout与完整FITS路径分开；SIF若保留premask副本则加做模型SHA对照。

本检查不代替DS/FITS科学质量验收。通过后查看DS与I/V cutout、核对实际命令与
观测时间/修正坐标，并记录Slurm Elapsed/MaxRSS。深度读取DS或全面参数扫描不作为
第一轮小闭环的前置条件。

### 6.1 本次归档硬链接失败的恢复

用户的首次single处理在extract_archive停止：新版旧检查一律拒绝链接。
现场只读诊断确认归档有88个成员、一个hardlink；
ASKAP_METADATA/footprintOutput-sb74372-src1-VAST_0535+00.txt指向同一MS中的
footprintOutput-sb74372-VAST_0535+00.txt，目标是归档中已有的普通文件。
修正后的统一检查允许这种内部直接硬链接，仍拒绝其他未支持的链接类型、
悬空/链式链接、越界路径、重复成员及特殊类型。解包继续使用命令行tar。
原始MS验收同时检查硬链接与目标samefile以及目标文件内容SHA和大小。

先按第3节从本地重新部署修正后的代码。原失败任务已经结束，继续使用现有
single_SB74372_beam28工作区及已下载tar/checksum，运行：

```bash
"$ASKAP_HOST_PYTHON" -u tests/validate_processing_stage3.py retry \
  --work-dir "$ASKAP_STAGE3_WORK" \
  2>&1 | tee stage3_single_retry_prepare.log
```

本次首次Archive links are unsupported恢复冻结一个新retry1，
保存旧metadata、失败报告、attempt/steps和日志证据至state/stage3-history/，
自动更新当前stage3-case.json引用。旧SQLite记录、下载和目录保留。
新计划沿用原frozen设置/SIF/源/数据集，冻结下载路径及SHA；缓存丢失或改变会失败，
不会登录CASDA、重新下载或自动建立retry2。正常主程序的默认重试策略没有改变。
重复执行retry会接管同一个恢复计划，准备中断后也不会重复创建attempt。

此时STAGE3 NOT_READY表示新attempt尚未运行，正常；最后应出现
STAGE3 RETRY PREPARED或STAGE3 RETRY ALREADY PREPARED。
确认后按第5节执行原run --submit命令，并保持controller运行。
不要再次prepare，也不需要再次download。完成后自动检查新的attempt；旧失败
仍可在历史报告及原run/attempt中查阅。阈值解析失败的后续恢复见下一节。
其他未识别故障或共享案例失败不适用此专用入口。

### 6.2 阈值参数失败后的恢复

现场retry1（run 20261013T145350-e94843070f5c、attempt
20261013T145350-ce2f5d2f1f90）已通过解包/预处理，但create-model退出2：
`--threshold 3.0`不符合SIF的INTEGER类型。UCDs不传此项，使用工具默认3；
mask-threshold的默认5也采用同一处理。新版默认命令已与UCDs逐项对照，
删除重复默认参数，保留数值3/5。完整记录见[参数核对](UCD_PARAMETER_AUDIT_zh.md)。

从本地按第3节再次部署更新，Ozstar继续使用同一单源工作区：

```bash
"$ASKAP_HOST_PYTHON" -u tests/validate_processing_stage3.py retry \
  --work-dir "$ASKAP_STAGE3_WORK" \
  2>&1 | tee stage3_single_parameter_retry_prepare.log
```

本次应出现STAGE3 RETRY PREPARED及integer-threshold，恰好一个独立retry2。
沿用原冻结设置/SIF、同一数据集/目标与下载SHA，复用已有raw MS，保留前两次
失败记录及日志。不需要重新prepare或download。NOT_READY表示等待执行，正常。

确认准备成功后显式提交，并保持controller运行：

```bash
"$ASKAP_HOST_PYTHON" -u tests/validate_processing_stage3.py run --submit \
  --work-dir "$ASKAP_STAGE3_WORK" \
  2>&1 | tee stage3_single_parameter_retry_run.log
```

仅日志中已确认的整数阈值解析失败可触发此纠正重试，不将其他建模问题误判为
参数问题，也不重置计数。retry2若仍失败则记录exhausted，不自动增加尝试或下载。
结束时自动检查；STAGE3 PASS后再进行共享案例。

## 7. 单源通过后，执行共享案例3B

后续在同一登录节点环境中使用独立目录，操作仍为prepare→download→run→check：

```bash
ASKAP_STAGE3_SHARED=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage3/shared3_SB74372_beam28

"$ASKAP_HOST_PYTHON" -u tests/validate_processing_stage3.py prepare \
  --case shared3 \
  --stage2-run "$ASKAP_STAGE2_RUN" \
  --work-dir "$ASKAP_STAGE3_SHARED" \
  --container "$ASKAP_ACCEPTED_SIF" \
  --python "$ASKAP_HOST_PYTHON" \
  --casda-username YOUR_CASDA_USERNAME \
  --password-file "$HOME/.config/askap/casda-password" \
  2>&1 | tee stage3_shared_prepare.log

"$ASKAP_HOST_PYTHON" -u tests/validate_processing_stage3.py download \
  --work-dir "$ASKAP_STAGE3_SHARED" \
  2>&1 | tee stage3_shared_download.log

"$ASKAP_HOST_PYTHON" -u tests/validate_processing_stage3.py run --submit \
  --work-dir "$ASKAP_STAGE3_SHARED" \
  2>&1 | tee stage3_shared_run.log

"$ASKAP_HOST_PYTHON" -u tests/validate_processing_stage3.py check \
  --work-dir "$ASKAP_STAGE3_SHARED" \
  2>&1 | tee stage3_shared_check.log
```

预期只有1个dataset/3个targets、8CPU、5.5h；首行种子加入两个范围外伙伴。
真实模型只建立一次，每个目标独立插模/减模/提取；有9件DS/I/V、正确不可变
版本和公开链接。单源与共享是两次独立处理，各自下载一次同一数据集。
完成后共享MS/模型副本按设计清理，history和日志仍用于证明共享；单源全部保留。
清理异常单独报告，科学成功不被改为失败。

3A/3B现均已通过。无需立即重复check及全部文件指纹计算；补充报告摘要，尤其是
独立的cleanup_status和Slurm资源记录。产物比较、四槽多轮、受控恢复和Ada传输
按[Stage4安排](STAGE4_PLAN_zh.md)继续。
