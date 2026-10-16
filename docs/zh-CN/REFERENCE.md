# 3. 运行机制

[索引](../../README.md) · [English](../en/REFERENCE.md) · [安装](INSTALLATION.md) · [使用](USAGE.md)

本页说明命令背后发生了什么：如何选择观测、如何处理每个目标、如何确定作业资源，
以及哪些文件会被保留。第一次运行不必阅读本页，但在解读结果或调整设置时会有帮助。

## 1. 职责划分

| 组件 | 运行位置 | 职责 |
| --- | --- | --- |
| `askap-batch` | 登录节点 | 读取源表，建立观测索引，按源的范围制定计划 |
| `askap-source` | 登录节点 | 查询单个坐标，写出候选观测表，按所选观测制定计划 |
| 控制程序（`run --submit`） | 登录节点 | 下载数据、提交 Slurm 作业、跟踪作业并续跑被中断的工作 |
| worker（在每个 Slurm 作业中） | 计算节点 | 解包归档，在镜像中运行 DStools，并记录每个目标的结果 |
| `pull_products.py` | 目标机器（Ada） | 拉取已发布的批量结果，并上传校验回执 |

两个命令共用 `askap_auto/` 中的程序，每次启动都会读取 `config.py` 和
`dstools-config.py`；程序本身没有默认值。计划会记录制定时使用的设置和范围。当命令
与未完成的计划不同时，它会列出差异，只有在你确认后、且该计划的 Slurm 作业都已停止
时才替换计划；旧计划作为历史保留。每个已提交的作业也保留自己运行时使用的设置副本。
每条命令都记录在 `logs/commands/` 中。说明文档中的“默认值”，指发布时这两个文件中
写好的值。

说明文档中使用的术语：

| 术语 | 含义 |
| --- | --- |
| SB | 调度块（scheduling block），即一次 ASKAP 观测，以 SB 编号标识 |
| beam | ASKAP 同时形成 36 个波束；CASDA 把每个波束的可见度数据存为单独的数据集 |
| 数据集 | 一个 SB、一个 beam 的可见度归档：打包为 `.tar` 的测量集（MS） |
| 目标 | 某个数据集中的一个源 |
| 伙伴 | 同一数据集中的另一个源表源；伙伴之间共用下载和天空模型 |
| 尝试（attempt） | 对一个目标的一次处理；重试和重算都会产生新的尝试 |
| 计划 | 一条命令将要处理的目标及所用设置，记录在 `state/runs/<运行ID>/` 中 |
| 动态谱（DS） | 目标亮度随时间和频率的变化，由 `dstools-extract-ds` 写出 |
| SIF | 包含 DStools 和 WSClean 的 Apptainer 镜像 |

## 2. 输入、筛选与观测身份

输入源表需要每个源的名称、赤经和赤纬（ICRS），可选自行与历元（见
[使用与参数](USAGE.md)第5节）。程序会保存读入的源表（`state/input_catalogue.csv`）
及其校验和与列映射。源表文件、列映射或单位改变后，下一条命令会读取新版本，把旧
版本保存在 `logs/catalogues/` 中，并刷新观测索引。

每个源的观测按三步选出：

1. **查询。** 通过 CASDA TAP 查询，取回波束中心在源的 `INITIAL_RADIUS_DEG` 范围内、
   且曝光时间超过 `EXPOSURE_MIN` 的可见度数据集。
2. **检查。** 每个候选都要通过下表的条件；未通过的条件会列在 `Eligibility_Reason` 中。
3. **选择。** 在每个 SB 中选择离源最近的合格 beam。若两个数据集距离相同，取数据集
   标识较小的一个；这种选择可以重现，但没有科学上的优先含义。批量处理这些选中的
   观测；单源计划可用 `--all-beams-for-sb` 把指定 SB 扩展到其全部合格 beam。

| 条件 | 默认值 |
| --- | --- |
| 数据 | 已公开的 ASKAP 可见度数据 |
| 质量 | `GOOD` 或 `UNCERTAIN`（`QUALITY_LEVELS`） |
| 曝光时间 | 严格大于 500 秒（`EXPOSURE_MIN`；`None`/`null` 表示不限制） |
| SB 范围 | 不设最小值（`MINIMUM_SB`），不排除任何 SB（`EXCLUDED_SBS`） |
| 归档大小 | 不超过 50 × 1024 × 1024 KB，即 50 GiB（`MAX_FILE_SIZE_KB`；可关闭） |
| 初始查询半径 | 1 度（`INITIAL_RADIUS_DEG`） |
| 每次查询的结果数 | 5000（`QUERY_LIMIT`）；达到上限会报错，需提高上限后重新查询 |
| 与波束中心的距离 | 在下面按频率缩放的半径之内 |

接受半径随频率缩放，近似为主波束宽度的一半：

~~~text
接受半径（度） = 0.5 × REFERENCE_FWHM_DEG × REFERENCE_FREQUENCY_GHZ / 频率（GHz）
~~~

按默认值（`REFERENCE_FWHM_DEG = 1.2`、`REFERENCE_FREQUENCY_GHZ = 1.4`），中心频率
0.8875 GHz 的观测接受半径为 0.5 × 1.2 × 1.4 / 0.8875 ≈ 0.95 度。
`RADIUS_FREQUENCY_MODE = "central"` 使用频段中心频率，`"top"` 使用频段上限，得到
的半径更小。初始查询半径只决定 CASDA 返回哪些候选：按默认值，只有所用频率低于
约 0.84 GHz 时接受半径才会超过 1 度。如果调大了接受半径，请同时调大
`INITIAL_RADIUS_DEG`。坐标或频率无效的记录，其 `Within_Radius` 为空，且不合格。

查询结果会被保存，在查询设置和源表都不变时重复使用。两者任一发生变化，下一条
`query` 或 `run` 都会重新查询所有源；`--update-query` 则在需要时主动这样做，用于
查找新发布的数据。

单源观测表列出每个候选及其被拒原因。被 CASDA 服务端直接过滤掉的数据集（例如曝光
过短）不会出现在表中。

数据集的身份由 SB、beam、归档文件名、CASDA 产品标识和频率范围共同组成一个 64 位
字符的标识（`Record_ID` 或 `Observation_Key`）。目录名写作
`SBxxxxx_beamyy--<标识前12位>`；后缀用于区分同一 SB、同一 beam 的不同数据产品，
不能只凭目录名前半部分判断是否重复。

## 3. 科学处理与目标隔离

批量处理中，每个数据集依次经过：

1. **下载**（登录节点）：从 CASDA 获取 `.tar` 归档及其 `.checksum` 文件。
2. **解包**（计算节点）：先检查归档结构，再解出原始测量集。
3. **共享模型**：MS 的一份副本依次经过 `dstools-askap-preprocess` 和
   `dstools-create-model`（用 WSClean 对整个视场成像和去卷积）。此步只做一次，
   由该数据集的所有目标共用。
4. **逐个目标**，在预处理后 MS 和模型的私有副本中：
   - `dstools-insert-model` 把目标周围一定区域（`MASK_RADIUS_ARCSEC`）屏蔽后预测
     天空模型，即视场中除目标以外所有源的模型；
   - `dstools-subtract-model --split-ms` 减去该模型，把残差写入单独的
     `.subtracted.ms`；
   - `dstools-extract-ds` 在经自行修正的目标位置，从残差的 `DATA` 列提取动态谱；
   - 从第3步生成的视场图像中，以目标为中心裁出 Stokes I 和 V 截图。
5. **发布与清理**：发布结果并删除临时副本（见第5节）。

重试和单源处理的步骤相同，只是每个目标使用私有模型，不共享。

以下设置位于 `dstools-config.py`：

| 设置 | 文件中的值与含义 |
| --- | --- |
| `MODEL` | `dstools-create-model` 的全部选项：DStools 默认值，只有 `band=AK_low`、`iterations=100000`、`multiscale=True`、`minuvw-m=200` 不同，用于重现我们原有流程的处理 |
| `MASK_RADIUS_ARCSEC` | `None`：使用 DStools 按波束确定的默认目标保护半径 |
| `EXTRACT_MINUV_M` | `500`：提取动态谱时使用的最短基线，单位米 |
| `EXTRACT_NOFLAG` | `False`：被标记（flag）的数据仍被排除 |
| `EXTRACT_BASELINE_AVERAGE` | `True`：对基线做平均 |
| `CROP_ARCMIN` | `10`：截图边长，单位角分 |

`MODEL` 中的每个值都会显式传给 DStools，因此处理结果不依赖镜像中 DStools 版本的
默认值。`None` 表示把该选项交给 DStools（例如由它计算图像尺寸和像素大小），开关为
`False` 时表示不加这个开关。提取动态谱时总是传入 `--datacolumn data`（残差）以及
`--baseline-average` 或 `--no-baseline-average`。每个计划的第一个作业会检查镜像是否
接受所用的全部选项。输出路径和文件前缀由程序管理。

**主波束图像**是可选的。只有一个目标时用 `PRIMARY_BEAM`；多个目标时用
`PRIMARY_BEAM_BY_SOURCE`，即从规范化源名到图像路径的字典。这些文件必须事先放在
OzSTAR 上，其校验和会记录在计划中。

**时间与自行。** 观测开始时间优先取自 CASDA；没有时，从测量集的 `TIME` 列读取，
并遵循其单位和时间尺度。坐标用 Astropy 从源表历元推算到该时间（不假设距离或
视向速度）。有自行但取不到可用时间的源，会在 `proper_motion` 步骤失败；没有
自行的伙伴照常处理。

**结果文件。** 每个目标、每个数据集产生：

| 文件 | 内容 |
| --- | --- |
| `<SB>_<beam>.ds`，例如 `SB12345_beam00.ds` | 动态谱 |
| `wsclean-MFS-I-image.fits`、`wsclean-MFS-V-image.fits` | Stokes I、V 截图，边长 `CROP_ARCMIN`，带天球 WCS |
| `observation.json` | 来源信息：源、SB、beam、尝试、设置版本、镜像校验和 |

在发布的批量结果中，两个截图位于 `wsclean_model/` 子目录，并沿用 WSClean 的
文件名，但它们是截图，不是全场图像。“完成”表示这些文件已生成并保存；数据在科学上
是否可用，需要由你判断。

## 4. 并行、时长与恢复

相互独立的数据集在并行的*槽*中处理；同一数据集的目标按顺序处理。默认 4 个槽、
每槽 8 线程，共 32 CPU；数据集较少的作业会少申请槽。内存（`MEMORY_GB`，每个作业
默认 160 GiB）在实际使用的槽之间平分，WSClean 可使用每槽份额的 75%。`TMP_GB`
（默认 10）是向 Slurm 申请的节点本地临时空间；测量集和模型都保存在工作目录中，
请据此规划 `/fred` 的存储。

作业时长按固定的工作量估算：

| 设置 | 默认值 | 含义 |
| --- | --- | --- |
| `HOURS_PER_OBSERVATION` | 2 | 准备一个数据集并建立其模型 |
| `HOURS_PER_TARGET` | 0.5 | 该数据集中每多一个目标 |
| `OVERHEAD_HOURS` | 2 | 每个作业的固定开销 |
| `RESERVE_MINUTES` | 20 | 作业末尾预留的有序退出时间 |
| `WALLTIME_HOURS` | 48 | 任何作业的时长上限 |
| `MAX_OBSERVATIONS_PER_JOB` | 40 | 每个作业最多包含的数据集数 |
| `MAX_CONTINUATIONS` | 20 | 作业到达时限后最多续跑的次数 |

数据集被分配到各个槽中；作业申请的时长是负载最重的槽的估计值，加上固定开销与退出
预留中较大的一个，并以 `WALLTIME_HOURS` 为上限。例如，一个数据集含三个目标时，
申请 2 + 3 × 0.5 + 2 = 5.5 小时；若已有可用的共享模型，则不计其中建模的 2 小时。
按默认值，一个作业最多处理同一数据集的 88 个目标，更大的组会拆到多个作业中。
这些估计不依据观测的曝光时间计算；`EXPOSURE_MIN` 只用于筛选观测。

**续跑。** 作业距结束只剩 `RESERVE_MINUTES` 时，worker 会中止正在运行的命令，把该
目标标为 `partial`，并不再开始新的工作。控制程序随后为未完成的目标提交续跑作业，
最多 `MAX_CONTINUATIONS` 次。已完成的目标、已建好的共享模型和已生成的动态谱都会
被复用，但被中止的那条命令会从头重新运行。如果某一步本身所需时间超过一个作业的
时长（典型情况是长观测的建模），每次续跑都会重复这一步，直到达到次数上限，目标
变为 `env_failed`，错误为 “Bounded walltime continuation exhausted”。遇到这种情况，
请在 `config.py` 中调大 `HOURS_PER_OBSERVATION` 后重新运行命令，这些目标会被自动
重试。

**作业失败。** 如果 Slurm 作业结束时没有留下 worker 的完成记录（例如节点故障、
作业被取消、内存不足，或模块、镜像问题），控制程序会把该作业中未完成的目标记为
`env_failed`（步骤 `worker`）。下一条命令会给它们新的尝试，不占用重试次数。节点故障
（`NODE_FAIL`、`BOOT_FAIL`）后，控制程序继续下一个作业；其他这类结束，以及 worker
报告空间不足或镜像检查失败时，控制程序会停止，并给出原因和 Slurm 日志位置，因为
下一个作业很可能以同样的方式失败。到达时限（`TIMEOUT`）或被抢占（`PREEMPTED`）的
作业则按上文的续跑方式继续。处理失败（如 DStools 某一步、截图或解包失败）记为
`failed`，只有 `--retry-failed` 才会重试（见[使用与参数](USAGE.md)第4节）。

控制程序需要保持运行，才能安排后续的下载和作业。它停止后，用相同的选项再次运行
同一条命令，或执行 `run --resume --submit`，即可恢复：继续原有计划，并接管已在
Slurm 中的作业。如果某次
提交的结果不确定，它会停下而不是重复提交，并要求你先核对 Slurm（见
[使用与参数](USAGE.md)第4节）。

## 5. 状态、结果与清理

`state/sb_processing.sqlite` 保存权威的处理状态；CSV 和 JSON 文件是从中导出的
视图，`progress` 会刷新这些文件并导入传输回执。

| 路径（相对于工作目录） | 内容 |
| --- | --- |
| `state/observation_progress.csv` | 当前及历史的“源—观测”状态、步骤、错误、日志和结果路径 |
| `state/processing_summary.json` | 查询、观测、共享组和各状态的数量 |
| `state/input_catalogue.csv` | 读入的源表，含规范化名称和解析后的坐标（批量） |
| `state/crosscatalogue_sources.csv` | 每个源的查询状态（批量） |
| `state/queries/<Source_ID>/` | 单个源的全部候选观测（批量） |
| `query/observations.csv` | 候选观测表（单源） |
| `logs/commands/` | 每条 `query` 和 `run` 命令，及其有效设置、来源和时间 |
| `logs/catalogues/` | 读入源表的旧版本（批量） |
| `state/runs/<运行ID>/execution-plan.csv` | 计划中的目标及其尝试标识；被取代的计划也会保留 |
| `state/runs/<运行ID>/effective-settings.json` | 有效设置及其来源 |
| `state/jobs/` | 作业清单、Slurm 脚本、提交与结束记录 |
| `logs/` | Slurm 输出和错误文件 |

数据存放位置：

~~~text
批量 WORK_ROOT/                       单源 WORK_ROOT/
├── state/      状态与表格            ├── state/
├── logs/       Slurm 输出、命令记录、├── logs/
│               源表旧版本            │
├── staging/    下载数据、共享模型、  ├── query/          候选观测表
│               各目标的处理副本      ├── observations/   全部数据与结果
└── products/   发布的结果            └── products/       （不使用）
                (PRODUCT_ROOT)
~~~

保留情况：

| 文件 | 批量 | 单源 |
| --- | --- | --- |
| 动态谱与 I/V 截图 | 发布在 `PRODUCT_ROOT` 下 | 位于尝试目录中 |
| 下载的归档（`.tar`） | 解包后删除 | 保留 |
| `.checksum` 文件 | 保留 | 保留 |
| 原始 MS 与共享模型 | 该数据集的所有目标都成功后删除 | 保留 |
| 每个目标的处理副本、模型和完整图像 | 结果发布后删除 | 保留 |
| 中断或失败的下载（没有对应的 `download-ready.json`） | 下次下载前删除；错误保留在 `download.log` 中 | 同批量 |
| 解包到一半的 MS（未记录在 `raw-ready.json` 中） | 重新解包前删除 | 同批量 |
| 未完成的共享模型构建（没有有效 `ready.json` 的 `build-*`） | 重新构建前删除 | （无共享模型） |
| 被中断的尝试留下的处理副本（`interrupted-work-*`） | 该目标再次处理时删除 | 同批量 |
| 被取代或因运行环境中止的尝试（`superseded`、`env_failed`）留下的处理副本 | 该目标再次准备时删除 | 同批量 |
| 处理失败（`failed`、`exhausted`）的输入数据 | 保留以便诊断；重新下载的重试会删除失败尝试的处理副本 | 保留 |
| 状态、日志和来源信息 | 保留 | 保留 |

完整的下载（记录在 `download-ready.json` 中且文件大小未变）和已解包的 MS（记录在
`raw-ready.json` 中）会直接复用，不会重新下载或解包。残留数据只由登录节点上的控制
程序或处理该数据集的作业删除，因此不会删除正在运行的作业所用的数据。

发布的批量结果路径：

~~~text
PRODUCT_ROOT/<Source_Name>/<数据集目录>
    -> PRODUCT_ROOT/.versions/<源ID>/<数据集ID>/<尝试ID>/
~~~

`.versions` 存放结果文件本身，且从不修改；可见路径是指向当前结果的链接。不要因为
它是隐藏目录就删除它。重算失败时旧结果仍是当前结果；只有新结果成功后链接才会
切换。复制整棵结果目录时，请同时保留链接和 `.versions`。

单源数据位于 `observations/<数据集目录>/`，每个尝试在 `attempts/<尝试ID>/` 中：
`products/`（动态谱）、`cutouts/`（I/V 截图）、`work/`（处理副本和完整图像，
包括 `work/wsclean_model/`）以及 `logs/`。清理失败会单独记录，不会撤销已保存的
结果。

## 6. Ada 传输与空间管理

`pull_products.py` 只选择已完成、已发布且三个文件（DS、I、V）齐全的批量结果。它
列出这些结果及其 SHA256 校验和，复制缺失或不一致的文件，逐一校验，并确认传输期间
OzSTAR 上的结果没有变化。全部通过后，才在目标机器登记完成并上传回执；在 OzSTAR 上
运行 `progress` 会导入回执，把这些结果标为 `verified`。

目标机器在 `.versions/` 下保存文件，建立指向当前结果的链接，并在
`state/transfer_ledger.sqlite` 中保存台账、清单和回执。重复拉取会复用已一致的
文件。两端都不会删除任何数据。

需要释放 OzSTAR 空间时，只删除已经拉取并校验过的结果（`Transfer_Status` 为
`verified`）。如果从未拉取过的结果丢失了，下一次制定计划会停止并提示
“Completed products unexpectedly missing”，直到你用 `--rerun-completed` 显式重算。

下载受 `STAGING_BUDGET_BYTES`（默认 2 TiB，统计 `WORK_ROOT/staging/` 下的全部内容）
和磁盘剩余空间限制。处理失败的数据会被保留，并计入预算；被中断或被取代的尝试留下
的残留会自动删除（见第5节）。如果某次下载、解包或复制会超出预算或剩余空间，相关
目标会被记为 `env_failed`，控制程序停止并给出提示。释放空间或调大
`STAGING_BUDGET_BYTES` 后重新运行命令，这些目标会被自动重试，不需要任何重试
选项。单源模式的数据保存在 `observations/` 下，不计入该预算，只受磁盘
剩余空间限制。
