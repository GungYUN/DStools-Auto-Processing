# Ozstar 部署与分阶段测试

本轮约定（2026-10-13）：

| 用途 | 路径 |
| --- | --- |
| 本地项目 | `/PATH/TO/DStools_Auto_Processing` |
| 建议远端代码目录 | `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing` |
| 宿主 Python | `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/env/askap-python/bin/python` |
| 建议独立验收根目录 | `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation` |
| 既有 SIF 候选，部署时核实 | `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/dstools/dstools-260925.sif` |

代码目录、验收数据目录与旧 `ASKAP-UCDs` 工作区分别管理。其他用户可替换
这些站点路径；批量设置在 config.py，单源设置在独立 JSON 或 CLI。
容器内 Python `/opt/askap/bin/python` 和 Ada 拉取所用的远端标准库 Python
与宿主科学环境用途不同，无需随此次宿主路径变更。

## 1. 上传与环境检查

本地终端执行，支持 SSH key/agent 或交互输入 Ozstar 登录密码：

```bash
cd /PATH/TO/DStools_Auto_Processing
./deploy.sh YOUR_OZSTAR_USER@ozstar.swin.edu.au /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
```

密码提示时输入 Ozstar 账户密码（终端不显示字符）；脚本的多个 SSH/rsync 连接
可能分别提示输入。脚本不保存密码。若出现认证失败，先单独执行
`ssh -o BatchMode=no YOUR_OZSTAR_USER@ozstar.swin.edu.au` 核实登录；登录成功后 `exit`
返回本地，再执行部署。早期脚本的 `BatchMode=yes` 禁用了密码提示，现已修正。

重复部署不会覆盖远端 config.py。若远端已有配置，手动将 PYTHON 更新为上表
路径，同时核对单源 JSON 的 python。活动运行恢复仍遵循冻结快照；在旧运行
结束后创建新运行以采用新配置，不直接修改快照。测试运行期间避免更新代码。

Ozstar 登录节点检查，不创建或重建已有 Python 环境：

```bash
cd /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
module load gcc/13.3.0 python/3.12.3 apptainer
ASKAP_HOST_PYTHON=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/env/askap-python/bin/python
test -x "$ASKAP_HOST_PYTHON"
"$ASKAP_HOST_PYTHON" -c 'import sys, numpy, astropy, astroquery, keyring; print(sys.executable); print(sys.version); print("numpy", numpy.__version__, "astropy", astropy.__version__, "astroquery", astroquery.__version__); assert sys.version_info >= (3, 10)'
"$ASKAP_HOST_PYTHON" -m pip check
"$ASKAP_HOST_PYTHON" askap-batch --help
"$ASKAP_HOST_PYTHON" askap-source --help
"$ASKAP_HOST_PYTHON" -m unittest discover -s tests -v
command -v apptainer sbatch squeue sacct rsync
df -h /fred/YOUR_PROJECT/YOUR_OZSTAR_USER
```

若实际缺少依赖，再用该解释器执行 `-m pip install -r requirements.txt`；先记录
已有包版本和安装输出。测试套件使用模拟服务，不会发起 CASDA 下载或提交 Slurm。
上述模块组合来自现有配置，仍需在站点确认与新 Python 环境兼容。

## 2. SIF 接口与计算节点环境

核实 SIF 路径与 SHA256；保存五个 DStools 命令的完整 help 输出及模块列表。
轻量计算节点诊断沿用：

```bash
sinteractive --time=02:00:00 --ntasks=1 --cpus-per-task=8 --mem=32G --tmp=32G
```

在分配的计算节点上加载同一模块，确认新宿主 Python 能导入依赖，并执行
`apptainer exec --cleanenv SIF路径 dstools-create-model --help` 等接口检查。
需检查 preprocess、create-model、insert-model、subtract-model、extract-ds
五个带 `dstools-` 前缀的命令，以及容器内 `/opt/askap/bin/python` 能导入
casacore。正式 worker 还会自动检查计划所需的具体选项；人工保留的输出用于
验收追溯。接口不匹配时先处理兼容性，不直接换掉已验收 SIF。

## 3. 单源最小闭环

先选一颗已有正式版参考结果的真实源，使用其原始坐标、历元及自行。
不要把示例 catalogue 的演示坐标当作已确认验收目标。

1. 在验收根目录下创建独立 `single_pilot` 工作区，将 examples/site.json 复制
   到验收根目录，填入已核实 SIF、CASDA 用户名和本地密码文件路径。
   密码文件权限为600，不上传密码内容。
2. 使用新宿主 Python 执行 askap-source query，检查 observations.csv 的公开性、
   质量、积分时间、频率、半径、最近 beam 及估计下载量。
3. 用 `--observation-key` 选择一个合格数据集。首次 run 不加 submit，冻结
   1槽×8线程计划，核对 JSON 和生成的 `.sbatch`：宿主 Python 必须是新路径，
   PYTHONPATH 必须指向新代码目录，所有输入输出位于独立验收工作区。
4. 再执行同工作区的 `run --submit`，使登录 controller 完成下载、提交和跟踪。
   单源保存 tar、原始 MS、完整模型和 FITS，先确认磁盘预算足够。
5. 对照正式版参考结果核查 DS/I/V、科学命令、观测时间、自行位置、原始 MS
   不变、完整 FITS 与 cutout 共存，以及 SQLite/CSV/日志一致性。

单槽测试仍需按真实 MS 大小估算内存和时间，不能假定32G或2小时一定足够。
2小时限制用于上述交互式诊断；普通 run 使用 sbatch 自动估时。若选择在2小时
交互分配内诊断科学步骤，必须使用适合该分配的数据和资源设置，不能直接套用
默认4槽计划。单数据集默认估计含预处理、目标处理、固定开销和退出余量，超过2小时。

## 4. 扩展验收

最小闭环通过后，在不同工作区逐项执行，避免之前的冻结计划影响选项：

| 场景 | 核查重点 |
| --- | --- |
| 单源两个 SB、其中一个多 beam | 显式展开的 SB 处理全部合格 beam；另一 SB 仅最近 beam |
| 三列 CSV/FITS 小 catalogue | 输入映射、原始行号、非 UCD 源名与产物目录 |
| 多目标共享同一数据集 | 一次预处理/建模，每个目标独立副本与 DS/I/V |
| 四槽多轮与墙钟续跑 | Slurm 资源、模型复用、续提上限、状态一致性 |
| 受控失败及成功重算 | 有限重试/重下载、旧成功版本保留、原始 MS 不变 |
| CASDA 时间缺失 | 真实 MS TIME 单位、参考尺度及自行位置回退 |
| Ada 独立验收目录拉取 | 三件套 SHA256、重复拉取、回执和状态登记 |

每项保留代码版本/校验清单、SIF SHA256、有效配置、Slurm job ID、日志及
检查结论。科学比较允许设计中明确的公开性筛选和自行修正差异，并解释差异来源。

用户提供的2026-10-13日志确认：新版已位于远端代码目录，登录节点 Python、
入口和46项模拟测试通过。随后Stage1/Stage2及真实单源/三源共享处理均已PASS，
记录见 [验证记录](VALIDATION.md)，剩余验收见 [Stage4安排](STAGE4_PLAN_zh.md)。

建议下一轮先完成3至5行真实小 catalogue 的输入与查询适配，使用独立工作区
比较三列 CSV、改列名/时分秒表及 FITS。核对输入快照、查询选择、范围与冻结
计划后，再用一个已知数据集完成单源真实链，最后用两个共享该数据集的目标
完成批量集成和一次 Ada 拉取。若 SIF 与正式版为同一已验收文件，可省略重新
构建及全面科学参数扫描；保留 worker 自动接口检查和真实集成任务。四槽压力、
人为中断与受控失败的独立现场验收归入可选Stage4C；结合原版验证基础及新版回归，
本轮跳过，不作为正式使用前置条件。实际证据与范围见[Stage4安排](STAGE4_PLAN_zh.md)。

第一阶段已提供人工构造的4行适配样本和离线脚本，执行指令见
[第一阶段说明](STAGE1_zh.md)。这些样本验证读取/标准化，真实查询阶段另选已知目标。
