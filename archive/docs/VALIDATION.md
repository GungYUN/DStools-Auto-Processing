# 实现与验证记录

更新日期：2026-10-15。代码已在本地完成实现与离线验证；用户提供的 Ozstar
日志确认新版代码已部署，登录节点环境及46项模拟测试通过，Stage1/Stage2
实际验收也已通过。随后用户提供Stage3A单源和Stage3B三源共享真实处理PASS；
Stage4A已执行且用户接受记录的数值差异；Stage4B的Ada真实传输闭环也已PASS。
结合原版验证基础及新版回归，本轮跳过独立Stage4C现场验证，不作为交付前置条件；
新版实际四槽、多轮调度与受控恢复现场状态保留为未执行。

当前宿主 Python 约定为 `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/env/askap-python/bin/python`，
建议远端代码目录为 `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing`。
环境检查和分阶段验收流程见 [部署与测试](DEPLOYMENT_zh.md)。
路径调整后重新运行新版46项测试，全部通过；另核对批量配置、单源默认值、
site.json 及实际生成的 `.sbatch` 均使用新宿主 Python。部署脚本 shell 语法检查通过。

## 已完成的本地验证

新增第一阶段站点验收脚本 tests/validate_catalogue_stage1.py，本地19项检查
通过；另使用非法坐标样本确认脚本退出码为1、报告为FAIL且指出源行号。
这19项是针对本阶段CSV/FITS样本的实际读取检查，与46项核心测试分开计数。
用户已提供Ozstar新宿主Python的第一阶段日志，19项全部PASS；独立工作区为
`/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage1/run-vrkpl7qn`。
指令见 [第一阶段说明](STAGE1_zh.md)。

2026-10-14准备第二阶段真实CASDA查询与计划检查脚本，指令见
[第二阶段说明](STAGE2_zh.md)。SIF仅用于读取SHA256，不执行容器；真实站点
用户随后提供真实复跑日志：Stage2共16项PASS、0项未覆盖，路径为
`/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage2/run-sxr6_93m`。
四源全部available，106条目标—观测关联、57个独立数据集、25个共享组。
精确共享key为1a0d2f532f040b634ebd43363ca54169062bf27d5e6d36e186e67f04a0eb3fe1，
三源共享SB74372/beam28，归档估计616591360字节；全部所需选择场景实际覆盖。
SIF SHA256为95a579cfe4e43ac48e19831270b6d81ad678f0ed5e601fd37106706d46aa7316。
第二阶段新增4项本地脚本驱动检查通过：完整CLI查询/规划路径、缺多beam时
返回INCOMPLETE、查询失败时返回FAIL，以及自洽但错误的角距离可被原始坐标
复核检出。本地仅模拟CASDA边界，不能替代接下来的Ozstar真实查询。

| 检查 | 结果 | 覆盖范围 |
| --- | --- | --- |
| 新版回归 `archive/tests/` | 149/149 通过 | 48项核心、7项有符号RA查询、4项Stage2驱动、7项公开性、7项Stage3驱动、13项归档链接、10项raw链接校验、13项恢复、9项模型类型、11项UCDs参数/资源、13项Stage4A、7项Stage4B |
| 历史回归 `archive/legacy/ozstar_askap/tests` | 66/66 通过 | 保留的正式版基线行为 |
| 核心 Python 3.10 语法解析 | 通过 | askap_auto 全部模块 |
| Ada 脚本 Python 3.7 语法解析 | 通过 | pull_products.py 及远端扫描程序 |
| CLI 帮助与 shell 语法 | 通过 | 两个入口、Ada 拉取、部署和 WSClean wrapper |
| 历史代码与原目录对比 | 无源码差异 | legacy/ozstar_askap 与原 Ozstar_ASKAP_UCDs/ozstar_askap |

测试使用维护者的本地宿主解释器（公开路径写作 `/PATH/TO/host-python/bin/python`）。新测试真实创建 SQLite、CSV/FITS、tar、
原始/私有副本和产品目录；CASDA、Slurm 和 DStools 可执行程序使用模拟器。
进程超时测试使用真实本地子进程；传输测试真实运行远端扫描代码并计算 SHA256，
通过本地传输适配器模拟网络。这不等于真实 MS 科学处理通过。

复现：

```bash
python -m unittest discover -s tests -v
(cd legacy/ozstar_askap && python -m unittest discover -s tests -v)
```

历史测试还需要 pandas；日常新核心无需 pandas。新测试额外验证：

- CSV 前导零、FITS 映射、原始行号、输入重排、名称冲突、非法自行。
- 时分秒和负赤纬、cosδ 约定、CASDA/MS 时间、缺失时间的逐目标隔离。
- 公开性先于最近选择；多 SB/beam 扩展；半径非法值；截断查询；稳定平局规则。
- 查询条件变化检测、失败结果保留与排除、逐源查询中断恢复。
- 全表共享与范围外伙伴、四槽实际并发峰值4及多轮处理、并行负载估时。
- 原始 MS 不变、预处理副本输入 insert、未掩膜模型保留、完整 FITS 与 cutout 共存。
- 科学失败保留输入、有限重试与重下载、修改参数跳过成功、失败重算保留旧成功。
- 不明确提交不重提、快速 worker 不被 queued 状态覆盖、完成目标仍等待 worker 终态。
- 墙钟 partial 续跑并复用模型、续提上限、损坏配置下按冻结快照恢复。
- 缺失 Stokes V 不完成；清理失败不抹去科学成功；产物目录不能跨工作区混用。
- 单源进度按执行选择计数；计划 ECSV 改动禁止下载；SIF 哈希和真实 CLI 支持检查接口。
- Ada 重复拉取零重复文件、损坏文件修复、源变化拒绝完成、回执上传失败不登记完成。

## 实施中定稿的约定

1. 新入口是 askap-batch / askap-source；通用核心 askap_auto 无批量配置导入依赖。
2. 旧 ozstar_askap、独立下载器和旧操作手册移至 legacy，仅作基线，不随新部署上传。
3. 数据集目录添加 `--<12位身份哈希>`；同 SB/beam 的不同数据集拥有独立目录。
4. 批量产品在不可变 `.versions` 目录中保存，以原子替换的符号链接发布当前版本。
5. 单源 run 不加 submit/prepare-download 时只生成计划；默认不提交、不下载。
6. 新增观测由显式 query 更新发现；普通 run 使用已保存的选择。
7. 第一版对同 beam 多数据集的 all-beams 歧义明确报错，不猜测或覆盖。
8. 分离减源 MS 中 residual DATA 为唯一提取数据列；不开放不相容的数据列。
9. 不共享多源 catalogue 的全局目标专属 mask；主波束文件支持按源映射。
10. 基线对比允许明确的科学修正：公开性顺序、自行单位/cosδ/历元及无固定日期回退。

## 仍需在站点执行的验收

用户提供的 farnarkle1 日志确认：宿主 Python 路径正确，版本3.12.3；numpy
2.5.4、astropy 8.0.1、astroquery 0.4.11 和 keyring 导入成功；pip check 无冲突；
两个入口帮助正常；46项测试全部通过（5.538秒）；apptainer、sbatch、squeue、
sacct、rsync 可找到。这是登录节点检查结果，测试中的远端服务和科学工具仍为模拟器。

用户随后提供的Stage2日志确认4颗源真实CASDA查询成功（available=4），
初步导出106条目标—观测关联、57个独立数据集、25个共享组；这些统计尚未通过
公开性复核。该次验收在第二项PASS后失败，工作区为
`/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage2/run-9z_xf6lq`。
当时尚无真实CASDA下载、Slurm提交、SIF help或真实MS/DStools执行结果。
其中 test_real_image_option_preflight_contract 验证的是模拟接口检查行为，不表示
实际 SIF 已被检查。后续先做小 catalogue 的输入/查询/计划适配，再完成最小真实
集成任务；不需重新全面验收同一已验证 SIF 的算法和全部科学参数组合。

quota 输出中 YOUR_PROJECT 的 /fred 组配额约剩13.3TiB、115000个文件额度，为组内共享；
df 显示的11P不能作为本项目可用额度。单源保留所有副本，下载前须估算空间及文件数。

### Stage2公开状态复核失败的修正

失败信息为Public flag differs from saved CASDA release metadata。本地已复现
以下机制：空obs_release_date在ECSV往返后成为masked值；Astroquery 0.4.11
直接以masked布尔表达式索引Table，可能将缺失发布日期的记录误选为公开。
原始TAP结果若已经masked也可能受影响，故不能只修改检查脚本。
机制与 [Astroquery 0.4.11官方源码](https://github.com/astropy/astroquery/blob/v0.4.11/astroquery/casda/core.py#L206-L220)
及 [Astropy ECSV缺失值说明](https://docs.astropy.org/en/stable/io/ascii/ecsv.html#masked-data)
一致；失败时具体差异行未提供，但修复后真实Stage2完整复跑已通过16项检查。

query.public_indices现先在副本上规范化发布日期：bytes解码，masked/None变空字符串，
再调用官方公开性筛选；缺少发布日期列明确报错，不猜测公开性。原始Table/ECSV不改。
新增7项回归在旧实现复现失败，修复后全部通过；全套57项在本地通过（4.256秒）。
Stage2复核失败时还会输出文件名、原始行号、发布日期/mask/dtype与两次public值。

更新部署后重新运行Stage2，自动创建新run目录，重新取得查询和公开标记。
旧目录保留作证据；普通run复用旧查询，不能将旧缓存的公开标记视为修复后的结果。

### Stage3准备

单源与三目标共享的独立真实处理安排见 [第三阶段说明](STAGE3_zh.md)。
从已通过的Stage2复制查询元数据，使用正式版目录的真实PM/J2000设置；只处理
SB74372/beam28，资源1槽×8线程、40GiB内存、10GiB tmp。准备、下载、提交及
文件/状态检查分步，SIF在计算节点自动验证实际接口。
新增7项Stage3本地驱动检查通过：单源/共享准备及资源/范围/真实PM设置、SIF或
Stage2结果不匹配时拒绝、未运行时NOT_READY，以及真实tar/SQLite配合模拟科学
程序的单源/共享完成检查与raw MS篡改检测。准备时的64项本地测试通过（5.867秒）。
模拟科学执行只证明检查器/流程契约，不作为真实MS科学处理通过。

用户首次Stage3单源日志确认prepare/download通过，CASDA登录及tar/checksum下载成功；
原run为20261013T141033-a1744af11586，attempt为20261013T141033-bee7e3684075。
计算在extract_archive前的成员检查停止，错误为Archive links are unsupported。
用户只读检查确认真实归档88个成员、1个内部hardlink，其目标是同MS内存在的普通文件。
修复后解包及checker共享统一成员规则，允许此直接硬链接，并验证内容SHA/大小和
samefile关系；13项归档、10项raw链接及11项恢复测试新增通过，全套98项通过（8.112秒）。
系统tar真实本地解包覆盖硬链接，实际SIF科学工具/CASDA/Slurm不属于新增离线测试。
专用retry入口保留旧失败证据、冻结原设置和下载SHA、仅准备新的单源retry1，
重复调用/两处metadata切换中断可恢复，缓存失效或再失败不重新下载。
部署更新及现场继续执行见 [Stage3硬链接恢复](STAGE3_zh.md#61-本次归档硬链接失败的恢复)。

用户随后日志确认retry1通过extract/preprocess，在create_model以退出2停止：
threshold的字符串3.0不符合SIF整数参数类型。已将默认科学命令对齐UCDs，
去除重复默认值和额外日志/目录参数；所有INTEGER选项统一校验，不做舍入。
基线CLI默认值和参数声明从已接受构建源码提取并记录来源SHA，通过真实Click
解析比较create/insert/subtract/extract全部生效值，默认值一致。
实际内存槽数分配与UCDs一致；批量私有模型不再额外复制一份。
新增参数/资源与第二次纠正恢复检查后全套121项通过（8.374秒），原47项核心
又单独通过以确认实际槽数少于配置上限的情况。
修正后的同一retry入口识别integer-threshold，冻结独立retry2，保留原冻结
设置及既有raw/download，继续显式提交。
调整明细见 [UCDs参数核对](UCD_PARAMETER_AUDIT_zh.md)。

### Stage3A/3B现场通过，下一步Stage4

用户随后提供 `STAGE3 PASS: required steps, retained files and fingerprints passed`，
报告路径为
`/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage3/single_SB74372_beam28/state/stage3-check.json`。
这确认单源的真实处理链、冻结身份、观测时间/自行记录、DS/I/V三件套、原始MS与
归档指纹、模型/完整图像/独立cutout保留协议通过。尚未收到完整JSON、Slurm耗时/
峰值内存和人工科学图像检查结果；不将文件协议PASS等同于DS/FITS科学内容验收。

用户随后提供Stage3B PASS，报告位于
`/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage3/shared3_SB74372_beam28/state/stage3-check.json`。
pipeline job为20261014T031211-97cfed0be715；共享history确认预处理与建模各一次，
耗时19.798秒和5121.703秒，实际1槽、40960MiB/槽；SIF SHA与已接受文件相同。
完整报告中的cleanup_status尚未提供，科学PASS与清理状态仍分别记录。

接下来先读取已有报告摘要/资源，比较Anchor的单源/共享结果，再完成Ada拉取及
默认并发/恢复验收，不重复已有正常路径。详见 [Stage4安排](STAGE4_PLAN_zh.md)。

Stage4A脚本与独立Slurm指令已完成，见[Stage4A说明](STAGE4A_zh.md)。新增13项
离线回归通过，全套134项在18.211秒通过；覆盖正式CLI在SQLite备份中跳过成功、
原状态保护、DS/FITS比较和篡改检测、SIF只读bind/失败明细、时间回退分支及accounting。
原生产模块与科学默认值未改。HDF5/时间/Slurm现场边界仍待Ozstar实际作业确认。

2026-10-15用户提供实际Stage4A输出：stage4a/run-vsulw3ip，Slurm 18585962，
33秒完成，10项PASS、0项FAIL、1项REVIEW。两种模式的清理、身份/科学输入、SIF、
普通入口跳过、真实MS时间回退/自行、Stage3资源记录及原状态保护均已通过。
唯一待复核项为accepted-SIF product comparison。用户随后提供完整比较JSON，
已保存为tests/stage4a-comparison.json：12件产物指纹通过，DS的时间/频率/UV轴、
属性、I/V的WCS及非有限值分布一致。DS flux相对RMS差异0.208944%，I为1.103531%，
V为0.000061866%；不能确认差异全部来自浮点尾数。用户随后明确接受这些已记录
差异并要求直接进入下一阶段，因此结束数值复核，不再采集日志或安排控制运行。
接受决定见tests/stage4a-acceptance.json，[数值记录](STAGE4A_REVIEW_zh.md)和原始
REVIEW报告保留；没有修改科学参数或全局比较容差。

Stage4B脚本/指令已完成，见[Stage4B说明](STAGE4B_zh.md)。新增7项本地测试使用真实
文件、台账和核心回执导入，SSH/rsync以本地传输边界模拟；全套141项通过。
本轮现场只检查首次9文件、重复0文件、Ada文件/台账/链接及Ozstar回执登记。
用户随后提供Ada Astro环境现场输出：五项全部PASS，最终STAGE4B PASS；工作区为
`/PATH/TO/ada/DStools_Auto_Processing_validation/stage4b/run-v16e3ccc`。
首次9文件、重复0文件，Ada文件SHA/大小、台账、回执和链接，以及Ozstar导入两份
新回执并登记三个目标均通过。完整report.json尚未复制到本地，现场结果依据用户
提供的控制台输出记录。不重新进行数值复核或正常传输测试。

### Stage4C范围调整：本轮跳过独立现场验证

2026-10-15用户提出依托原UCD版验证基础跳过4C。代码核对确认两版保留相同的
按槽分配数据集、Slurm提交及有限恢复思路，但新版controller/worker/state已有
run/attempt、身份和结果版本适配，不能称为未经修改的原调度代码。
为核实已有覆盖，复跑tests/test_auto.py中14项相关既有回归，全部通过：
四槽负载与真实线程多轮执行、实际槽数资源、sbatch生成、不明确提交保护、快速worker、
partial/模型复用、续提上限、冻结计划恢复、活动作业等待、有限重试、失败重算保护、
重算范围及分块模型复用。这14项属于已记录的141项，不新增测试计数。
使用真实本地线程、SQLite和文件，外部科学/调度服务仍模拟；不标作新版现场4C通过。

结合Stage1至4B现场结果，决定结束本轮发布前验证，4C改为可选专项检查。
若后续确有需要，可在独立 WORK_ROOT/PRODUCT_ROOT 中执行以下场景，
参见[Stage4安排](STAGE4_PLAN_zh.md)：

1. 至少5个独立数据集的真实四槽/多轮处理，核对Slurm资源、实际并发及进度。
2. controller中断/恢复，不重复提交活动任务、不重算成功记录。
3. 受控墙钟partial续跑、模型复用及续提上限。
4. 受控单目标失败隔离、显式有限重试、至多一次重下载及最终耗尽。
5. 隔离样本的显式重算，旧成功版本与指针在新尝试失败时保持有效。

这些场景本轮不执行，也不作为正式使用门槛。已有Stage2选择规则、Stage3正常处理、Stage4A清理/资源/时间回退/成功跳过和
用户接受的产物比较、Stage4B传输闭环均不再列为待执行项。

交互式验证资源约定保持：

```bash
sinteractive --time=02:00:00 --ntasks=1 --cpus-per-task=8 --mem=32G --tmp=32G
```

交互式验证的测试计划须设为1槽、8线程且不超过实际2小时分配；生产 sbatch 使用
配置的资源与自动估时，上面的验证标准不替代生产资源设置。
