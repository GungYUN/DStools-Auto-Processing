# 第二阶段：真实查询与冻结计划

第一阶段Ozstar实际结果为19项PASS，工作区：
`/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage1/run-vrkpl7qn`。

本阶段在登录节点查询公开CASDA元数据，保存ECSV/CSV/JSON和处理计划。
需要现有SIF用于计算SHA256，不执行Apptainer、DStools、下载或sbatch。
无需CASDA账户/密码。脚本有执行拦截保护，所有计划使用新建的独立工作区。

## 1. 本阶段目标

`examples/validation/stage2_targets.csv`包含以下真实目录参考位置：

| 测试名 | 对应基线源 | RA度 | Dec度 | 历史验收参考 |
| --- | --- | --- | --- | --- |
| Anchor A | UCS689 | 84.5194 | -2.2313 | SB74372 beam28共享组 |
| Partner+02 | UCS690 | 84.5421 | -2.6072 | 同上 |
| Partner-03 | UCS693 | 84.5596 | -2.7532 | 同上 |
| Edge Check | UCS225 | 24.2357 | 9.5631 | SB67772 beam18半径边缘 |

位置来自保留的正式版 `UltracoolSheet_Version2.1_index_unbinaryUCD.csv` 的
ra_j2000_formula/dec_j2000_formula。Reference_Name/Reference_ID仅作溯源列，
程序只映射Name、RA、DEC，不以UCS或光谱型筛选。历史案例记录见
`legacy/ozstar_askap/GATE8_CENTRAL_VALIDATION.md`。当前查询以CASDA实际返回为准。

本阶段采用零自行及J2000参考位置，用于查询/计划适配，不能直接用于验证
这些真实源的观测历元位置。第三阶段创建新工作区，按实际自行/历元、资源
和已选数据集重新定稿科学设置；本阶段计划保留作为检查证据。

## 2. 本地更新上传

```bash
cd /PATH/TO/DStools_Auto_Processing
./deploy.sh YOUR_OZSTAR_USER@ozstar.swin.edu.au /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
```

## 3. Ozstar登录节点执行

SIF默认使用正式版已有路径。若已验收文件位于其他路径，替换ASKAP_ACCEPTED_SIF。

```bash
cd /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
module load gcc/13.3.0 python/3.12.3 apptainer
ASKAP_HOST_PYTHON=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/env/askap-python/bin/python
ASKAP_ACCEPTED_SIF=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/dstools/dstools-260925.sif

set -o pipefail
"$ASKAP_HOST_PYTHON" -u tests/validate_query_stage2.py \
  --container "$ASKAP_ACCEPTED_SIF" \
  --python "$ASKAP_HOST_PYTHON" \
  --output-root /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage2 \
  2>&1 | tee stage2_query_validation.log
```

不用编辑根config.py。脚本生成独立批量配置与单源JSON，在脚本进程内忽略
原会话ASKAP_*覆盖，退出后保留原会话环境。预计实际查询4个批量目标和
1个相同位置的单源；普通重复query验证及beam扩展使用已保存的查询。
查询耗时取决于服务响应，SIF哈希也需要读取完整文件，均在日志中逐步显示。

## 4. 自动检查

- 逐源真实查询成功；查询失败不能计为零观测成功。
- 完整候选表与原始ECSV指纹/数量；公开性、质量、大小、曝光、频率和半径规则。
- 每个SB仅选最近的合格beam；批量压缩索引与完整候选表一致。
- batch的普通query使用冻结索引；单源真实query与批量相同位置的默认选择一致。
- begin=1/end=1只作为种子范围；限制一个精确数据集，并纳入同数据集的范围外伙伴。
- 单源默认选择；另一个独立工作区展开一个有多合格beam的SB，其他SB仍最近选择。
- 活动计划恢复保持原冻结范围；新Python和代码路径、CPU/内存、目录隔离与SIF哈希正确。
- jobs状态为planned，attempts为preparing；无处理steps、归档/MS、scheduler ID或提交回执。

规划会正常生成 `.sbatch` 和job manifest，这不表示提交了作业。测试资源设置为
1槽×8线程、32GiB内存、32GiB tmp，48小时上限内自动估时，仅用于检查计划。

## 5. 结果与下一步

末尾有以下三种结果：

| 结果 | 意义 | 退出码 |
| --- | --- | --- |
| STAGE2 PASS | 所有检查通过，共享和多beam场景实际覆盖 | 0 |
| STAGE2 INCOMPLETE | 已执行项通过，但当前查询没有覆盖共享、多beam或第二个SB | 2 |
| STAGE2 FAIL | 查询、配置或计划检查失败，需根据错误定位 | 1 |

缺少某个场景时不会用模拟数据补成PASS，也不会要求历史SB一定重现。
报告每项已通过/未覆盖的检查，保留源候选数、合格数、选择数及具体错误。

首次站点执行的4个真实查询均成功，但公开状态复核失败。已修正发布日期
masked/空值经ECSV往返后的筛选兼容问题，详情见 [验证记录](VALIDATION.md)。
更新部署后再次执行上面的完整命令，让脚本在新run目录重新query；保留原失败
目录run-9z_xf6lq供追溯。修复未放宽公开性要求；缺失发布日期的记录明确排除。

每次在stage2根目录创建新run目录，包含inputs、batch_range、single_nearest、
（条件满足时）single_expanded、report.json和plan-locations.json。
批量全候选表在batch_range/state/queries/<Source_ID>/observations.csv，单源全表在
single_nearest/query/observations.csv。报告/配置/计划均保留便于审阅。

请提供stage2_query_validation.log的输出及末尾报告路径。根据这次实际查询的
合格数据集和估计大小，确定第三阶段的一源一数据集与共享小批量，而不是直接
执行本阶段的全SB测试计划。
