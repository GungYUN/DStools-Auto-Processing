# 通用批量与单源工具操作说明

本项目只处理 ASKAP。登录节点查询、下载并提交 Slurm；计算节点离线运行
DStools。不要在旧 UCD 工作区执行新程序。以下宿主 Python 和部署目录采用本次
Ozstar 账户的约定；其他用户需换成自己的路径/账户。部署验收步骤见
[部署与测试](DEPLOYMENT_zh.md)。

## 1. 安装与配置

在 Ozstar 登录节点加载适合站点的模块，使用已有环境：

```bash
module load gcc/13.3.0 python/3.12.3 apptainer
export PATH="/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/env/askap-python/bin:$PATH"
python -m pip check
# 仅在依赖检查表明缺包时安装：
# python -m pip install -r requirements.txt
```

之后的 `python` 指 `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/env/askap-python/bin/python`。
批量配置、单源默认值和 site.json 示例均使用该路径；其他用户可覆盖。
使用已有验收 SIF；无需为了本项目重新构建容器。计算节点不安装软件或联网下载。

本地部署到独立代码目录（支持 SSH key/agent 或交互输入 Ozstar 登录密码）：

```bash
./deploy.sh YOUR_OZSTAR_USER@ozstar.swin.edu.au /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
```

脚本不上传旧 UCD 程序、数据、运行状态或凭据，重复部署不覆盖远端 config.py。
已有远端 config.py 或单源站点 JSON 时，应同步更新其中的宿主 Python 路径。
已冻结的活动运行仍使用原快照，不会自动采用新路径；不要直接改写运行快照。
密码文件由用户在目标机器创建，权限设为600；程序不接受命令行明文密码。
CASDA 账户通过 `CASDA_USERNAME` 或单源 JSON 的 `casda_username` 提供，密码
默认读取 `~/.config/askap/casda-password`，可显式配置其他文件。登录进程也可
通过 `CASDA_PASSWORD` 临时提供密码；该值不写入配置快照或计算任务。

## 2. 批量输入

编辑根目录 `config.py`，至少设置 WORK_ROOT、CATALOGUE_PATH、CONTAINER、
PYTHON、CASDA_USERNAME。PRODUCT_ROOT=None 使用 WORK_ROOT/products。
必需列只有源名、RA、Dec；`examples/targets.csv` 是最小示例。

不同列名、时分秒和可选自行的例子：

```python
CATALOGUE_COLUMNS = {
    "source_name": "Object", "ra": "RightAscension", "dec": "Declination",
    "pmra": "MuAlphaStar", "pmdec": "MuDelta", "epoch": "Epoch",
}
RA_UNIT = "hourangle"
DEC_UNIT = "deg"
PM_UNIT = "mas/yr"
PMRA_INCLUDES_COSDEC = True
```

FITS 输入设置 `CATALOGUE_FORMAT="fits"` 和 `CATALOGUE_FITS_HDU=1`。每行参考
历元可映射 epoch 列，否则采用 COORDINATE_EPOCH。仅支持 ICRS。
未配置自行列为零；已配置列有缺失/非法值会报具体行号。

Source_Name 用作目录：空白连续段转换为一个下划线，接受 ASCII 字母/数字、
`_ . + -`，首字符必须字母/数字，不能含 `..`。保留原名、前导零和源身份。
使用不同 catalogue、重排或改名时创建新工作区，不覆盖原输入映射。
路径须为不含空白和 shell 元字符的绝对路径，这是 DStools 内部 shell 的限制。

配置优先级是 CLI > `ASKAP_<设置名大写>` 环境变量 > config.py > 默认值。
环境中的列表/字典/布尔值使用 JSON，例如 `ASKAP_PARALLEL_SLOTS=2`。
所有有效值和覆盖来源保存到 effective-settings.json。旧 `ASKAP_WORK` 仅作为
WORK_ROOT 的显式兼容别名；其他旧 UCD 环境名称不属于新接口。

```bash
python askap-batch query
python askap-batch run --begin 1 --end 20                 # 只生成冻结计划
python askap-batch run --submit                         # 执行或恢复该计划
python askap-batch progress --work-dir /path/to/work
python askap-batch failures --work-dir /path/to/work
```

范围包含两端，默认全表。全表先建立查询索引，共享同一数据集的范围外伙伴可加入
普通处理；不会递归扩展伙伴的其他观测。成功重算不会顺带重算范围外的成功伙伴。
`--source Name` 可重复，按规范化名称选择；`--observation-key HASH` 可限制数据集。

普通运行复用查询索引；发现新增观测需显式更新：

```bash
python askap-batch query --update-query
python askap-batch run --submit
# 只重试查询失败的源，不重新查询已经成功的源：
python askap-batch query --retry-query-failures
```

查询设置改变时必须显式更新。查询失败不等于没有观测；旧行可保留用于追溯，
不会被当成本轮成功查询参与调度。活动运行未结束前禁止更新查询。

## 3. 单源两步流程

单源工具不导入批量 config.py，也不继承 ASKAP_* 环境配置。query 不下载、不提交。

```bash
python askap-source query --ra "12:00:35.856" --dec="-28:36:57.24" \
  --ra-unit hourangle --dec-unit deg --name MySource --work-dir /path/to/MySource
```

十进制度默认无需单位参数。名称可省略，自动生成坐标名称并保存精确坐标。
可添加 `--pmra 100 --pmdec -20 --coordinate-epoch J2000`。不包含 cosδ 的 pmra
用 `--no-pmra-includes-cosdec` 明确指定。

查看 `query/observations.csv` 及 `observations-columns.md`。表中保留各 beam 候选、
公开性、质量、频率、半径、Eligible、拒绝原因和默认最近选择；TOPCAT 可直接读取。
默认半径按频率上下限的算术平均计算；只有合格记录才能被选中。

独立站点 JSON 参见 `examples/site.json`。执行：

```bash
python askap-source run --work-dir /path/to/MySource --settings /path/to/site.json
python askap-source run --work-dir /path/to/MySource --submit
```

第一条不加 submit 时仅冻结计划；第二条恢复该计划，不需重输站点/科学参数。
也可在第一条直接加 `--submit`。指定多个 SB 扩展全部合格 beam：

```bash
python askap-source run --work-dir /path/to/MySource --settings /path/to/site.json \
  --all-beams-for-sb 35429 21677 --parallel-slots 4 --submit
```

扩展选项在创建计划时提供。其他 SB 仍处理最近的一个合格 beam，不是 SB 白名单。
不存在的 SB、无合格记录或同 beam 的多数据集歧义会在下载前报错。
run 不重新查询 TAP；新观测集合需在活动任务完成后再次执行 query。

## 4. 参数与恢复

常用科学/资源选项：`--container`、`--parallel-slots`、`--threads-per-slot`、
`--memory-gb`、`--walltime-hours`、`--crop-arcmin`、`--mask-radius-arcsec`。
模型参数使用完整长选项名，例如：

```bash
python askap-source run --work-dir /path/to/MySource --settings /path/to/site.json \
  --model-option robust=-0.5 --model-option imsize=4096 \
  --set extract_minuv_m=500 --submit
```

默认科学参数与UCDs正式版一致。config.py的MODEL只列原命令显式设置的band、
iterations、multiscale和minuvw-m；其他可配置项的默认值不重复传给DStools。
需要修改时再添加键或使用--model-option。整数选项（包括threshold/mask-threshold）
拒绝小数和布尔值，不进行舍入。参数对照及冗余清理见[检查记录](UCD_PARAMETER_AUDIT_zh.md)。

`--set KEY=VALUE` 使用 settings.py/config.py 的小写键名。JSON `null` 表示 None，
例如 query 的 `--set exposure_min=null` 或 `--exposure-min null`。
通用选项不可绕过数据管理：前缀、输入/输出目录由程序控制；减源固定生成独立
.subtracted.ms，提取限定 residual DATA。交互掩膜和其他望远镜参数不开放。
模型 phasecentre 如配置为 `[RA_deg,Dec_deg]` 会进入模型身份；留空使用输入 MS。
外部文件必须已存在并可被容器访问；其哈希在执行前校验，不在计算节点下载。
多源 catalogue 的主波束文件使用 PRIMARY_BEAM_BY_SOURCE，不隐式共用目标文件。

普通运行跳过成功和确定失败的记录。两个显式操作分别是：

```bash
python askap-batch run --retry-existing --submit
python askap-batch run --source Example_A --rerun-completed --submit
# 单源入口也支持同名选项。
```

显式失败重试：从原始 MS 私有复制开始完整处理一次；仍失败则重新下载并处理一次；
之后耗尽，不无限循环。单源保留所有失败输入和旧尝试。显式成功重算在独立尝试中
执行，旧成功产物保留，新结果全部成功后才切换当前版本。普通参数修改不会自动重算。

活动任务恢复使用原计划中的范围、beam 选择与有效设置；修改 config.py 或新传参数
不会改变该运行。需要新参数的运行应等当前运行完成后创建。登录 controller 需持续
运行以提交后续批次；可在站点允许的持久会话中运行。掉线后同目录 run --submit 恢复。

提交与回执写入之间中断时，程序会停止并报告不明确提交，不能自动再次 sbatch。
先检查 Slurm，再选择其中一条：

```bash
python askap-source resolve-submission --work-dir /path/to/MySource \
  --job PIPELINE_JOB_ID --scheduler-id EXISTING_SLURM_ID
# 仅在已核实没有创建任何 Slurm 作业时：
python askap-source resolve-submission --work-dir /path/to/MySource \
  --job PIPELINE_JOB_ID --confirmed-not-submitted
```

随后 run --submit。批量入口支持相同管理命令。不要删除正在占用的锁文件。

## 5. 结果、进度与 Ada

`state/observation_progress.csv` 自动导出当前及历史源—观测状态，包含未开始记录、
各步骤、失败原因/日志、时间来源、参数/模型版本和 DS/I/V 路径。`processing_summary.json`
区分查询失败、已知观测、共享组及成功/失败数量。完成判定仅检查规定产物的存在性，
不代表科学质量验收。Slurm COMPLETED 也不等于全部源成功。

批量可见产品目录含数据集哈希后缀，通过受管理符号链接指向 `.versions` 中的不可变
版本；不要删除隐藏版本目录。单源原始/处理中间数据和完整 FITS 均保留，cutout 独立。
全部 MS 与大模型位于持久工作区；tmp 只用于小型临时文件。

在 Ada 上复制并运行独立的 `pull_products.py`（不需要安装科学依赖）：

```bash
python3 pull_products.py --user OZSTAR_USER \
  --remote-work /fred/PROJECT/USER/askap-work \
  --remote-products /fred/PROJECT/USER/askap-work/products \
  --destination /PATH/TO/ada \
  --password-file ~/.config/askap/transfer-password
```

密码文件可省略以使用 SSH key/agent。也可把 config.py 复制到 Ada，并用
`python3 pull_products.py --config /path/to/config.py` 读取其中的 WORK_ROOT、
PRODUCT_ROOT、OZSTAR_USER、OZSTAR_HOST、ADA_ROOT、TRANSFER_PASSWORD_FILE；
CLI > ASKAP_同名环境变量 > 配置。站点用户名和根目录必须配置。
默认拉取所有完整批量产品，可重复加 `--source NAME`。逐文件检查 SHA256/大小，
只复制缺失或不匹配文件；三件套验证、源稳定性检查和回执上传全部成功后才登记完成。
进度命令会将回执导入 Ozstar 状态库。单源完整图像/中间数据传输不属于此接口。
当前没有自动删除已传输产品的命令；后续管理必须保留对应的状态和回执。
