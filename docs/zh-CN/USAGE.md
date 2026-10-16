# 2. 使用与参数

[索引](../../README.md) · [English](../en/USAGE.md) · [安装](INSTALLATION.md) · [运行机制](REFERENCE.md)

本页依次说明批量处理（第2节）和单源处理（第3节），然后介绍进度、修改设置与重试
（第4节）、参数设置（第5节）和结果拉取（第6节）。开始前请先完成[安装](INSTALLATION.md)。

两种流程都分三步：

| 步骤 | 命令 | 作用 |
| --- | --- | --- |
| 1. 查找观测 | `query` | 在 CASDA 中查询并记录候选观测；不下载、不提交作业 |
| 2. 制定计划 | `run` | 显示将处理哪些目标和观测以及下载量，并记录这份计划；不下载 |
| 3. 执行 | `run --submit` | 下载数据、提交 Slurm 作业并跟踪到全部结束 |

`run --prepare-download` 是介于两者之间的选项：只下载下一个作业所需的数据并
写出其 Slurm 脚本，但不提交。

> **每条命令都会重新读取设置。** 两个命令每次启动都会读取 `config.py` 和
> `dstools-config.py`。中断后要继续，请**用相同的选项再次运行同一条命令**，或者
> 使用 `run --resume`，它会重复未完成计划启动时使用的选项。如果设置和选项都没有
> 变化，命令会继续原有计划，并接管已经提交的 Slurm 作业。只要有任何不同（修改过
> 设置文件，或 `--begin`/`--end`、`--source`、`--all-beams-for-sb`、
> `--retry-failed` 等选项不同），命令会列出差异并询问
> `Replace the unfinished plan? [y/N]`（是否替换未完成的计划）。回答 `y` 即按当前
> 设置重新制定计划，已完成的结果会保留；其他任何回答都会保持未完成的计划不变。如果
> 旧计划的 Slurm 作业仍在排队或运行，命令会拒绝执行，并提示你先等待作业结束或将其
> 取消。上一份计划已经全部完成时，没有需要替换的内容，命令会直接运行而不询问。每条
> 命令及其有效设置和来源都记录在 `WORK_ROOT/logs/commands/` 中。

`run --submit` 启动的进程（*控制程序*）会一直在登录节点运行到所有作业结束，
可能持续数天。请在 `tmux` 中启动，断开连接后它仍会继续运行（见第1节）。

## 1. 每次登录的环境

每次登录 OzSTAR 登录节点后执行（替换占位值）：

~~~bash
module load gcc/13.3.0 python/3.12.3 apptainer
cd /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools-Auto-Processing
ASKAP_HOST_PYTHON=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/askap-python/bin/python
~~~

下文所有命令都在这个程序目录中执行，并以 `"$ASKAP_HOST_PYTHON"` 开头，即
[安装](INSTALLATION.md)第3节建立的 Python 环境。

### 保存 CASDA 密码（只需一次）

下载数据需要 CASDA（OPAL）密码。密码不写在 `config.py` 中，程序在处理过程中也不会
提示输入，所以请在第一次 `run --submit` 之前保存一次：

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch set-casda-password
~~~

该命令请你输入两次密码（屏幕不显示），并把它保存在 `config.py` 的
`CASDA_PASSWORD_FILE` 所指定的文件中（默认 `~/.config/askap/casda-password`），
该文件只有你本人可读。如果 `config.py` 中已经设置了 `CASDA_USERNAME`，命令随后会
测试能否登录；若提示 `CASDA authentication failed`，可能是用户名或密码有误，也可能
是网络问题。密码更改后重新运行该命令即可；`askap-source set-casda-password` 的作用
相同，加 `--no-check` 可跳过登录测试。也可以不用密码文件，而在运行控制程序的 shell
中导出 `CASDA_PASSWORD`，它优先于密码文件。两者都没有时，下载会停止并提示
“CASDA credentials are missing”。查询（`query`）不需要密码。

### 长时间运行与 Slurm

长时间运行的处理请先打开 `tmux`，并在其中执行上面三行：

~~~bash
tmux new -s askap        # 新建名为 askap 的会话
# ……执行命令……；按 Ctrl-b 再按 d 断开
tmux attach -t askap     # 之后重新连接
~~~

`tmux` 会话只存在于一个登录节点上。若有多个登录节点，请记下节点名（`hostname`），
重新连接时登录同一节点。

第一次运行前，也可以先确认 Slurm 能以你的账户接受默认作业规模（32 CPU、160 GiB
内存）；下面的命令不会真正提交作业：

~~~bash
sbatch --test-only --time=01:00:00 --cpus-per-task=32 --mem=160G --wrap=hostname
# 如果你的分配需要，再加上 --partition=... 和/或 --account=...
~~~

## 2. 批量处理源表

### 准备配置和源表

两个命令都从程序目录中的两个文件读取设置：`config.py`（数据、查询、Slurm 和账号
设置）与 `dstools-config.py`（DStools 参数和截图尺寸，见第5节）。两个文件都列出了
各自的设置项和简短说明。程序没有内置默认值，所以每项设置都要保留在这两个文件中
（可选的值写 `None`）。用编辑器打开 `config.py`（例如 `nano config.py`），至少填写
以下四项：

| 设置 | 含义 | 示例 |
| --- | --- | --- |
| `WORK_ROOT` | 批量工作目录（不存在时会自动创建） | `"/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue"` |
| `CATALOGUE_PATH` | 你的源表，CSV 或 FITS | `"/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/catalogues/targets.csv"` |
| `CONTAINER` | DStools 镜像 | `"/PATH/TO/dstools.sif"` |
| `CASDA_USERNAME` | 你的 CASDA（OPAL）登录名 | `"YOUR_CASDA_USERNAME"` |

CASDA 密码不写在 `config.py` 中，请用 `askap-batch set-casda-password` 保存一次
（见第1节）。

以下几项也经常需要设置：

| 设置 | 含义 | 文件中的值 |
| --- | --- | --- |
| `PYTHON` | Slurm 作业中使用的解释器，计算节点必须能读取 | 启动命令的解释器 |
| `PARTITION`、`ACCOUNT` | Slurm 分区和账户，你的分配需要时填写 | 不设置 |
| `MODULES` | 每个作业开始时加载的模块 | `["gcc/13.3.0", "python/3.12.3", "apptainer"]` |
| `PRODUCT_ROOT` | 发布结果的位置 | `WORK_ROOT/products` |
| `BEGIN`、`END` | 要处理的源表行（从1开始，包含两端） | 整张表 |

例如，需要修改的几行可以是：

~~~python
WORK_ROOT = "/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue"
CATALOGUE_PATH = "/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/catalogues/targets.csv"
CONTAINER = "/PATH/TO/dstools.sif"
CASDA_USERNAME = "YOUR_CASDA_USERNAME"
~~~

其余设置没有特别理由时保持原值即可。常用设置见第5节，全部设置的说明见两个文件
中的注释。之后发现设置有误时，修改相应文件并重新运行命令即可（见第4节）。由于这
两个文件位于程序目录中，更新程序时如何保留修改见[安装](INSTALLATION.md)第2节；
填有个人路径的设置文件不要提交或推送。若要为多张源表分别保存设置，可以把其他设置
文件放在仓库以外，使用时用 `--config /PATH/TO/my_catalogue.py` 指定；此时 DStools
参数从同一目录下的 `dstools-config.py` 读取，或从 `--dstools-config` 指定的文件
读取。

### 准备源表

源表每行一个源，需要名称、赤经和赤纬（ICRS）。按默认设置，列名为 `Name`、
`RA`、`DEC`，坐标都以十进制度表示，如 [targets.csv](../../examples/targets.csv)：

~~~csv
Name,RA,DEC
Example A,180.1494,-28.6159
Example_B,180.25,-28.65
~~~

**自行（可选）。** 可以为每个源加上自行以及该坐标对应的历元。程序会在提取
动态谱之前，把每个位置推算到各次观测的时间。在源表中加入这些列，并在
`config.py` 中告诉程序列名：

~~~csv
Name,RA,DEC,PMRA,PMDEC,EPOCH
Example A,180.1494,-28.6159,-120.5,35.2,J2016.0
Example_B,180.25,-28.65,0,0,J2016.0
~~~

~~~python
CATALOGUE_COLUMNS = {"source_name": "Name", "ra": "RA", "dec": "DEC",
                     "pmra": "PMRA", "pmdec": "PMDEC", "epoch": "EPOCH"}
~~~

默认情况下，`PMRA` 表示 μα cos δ，两个自行的单位都是 mas/yr。历元写成
`J2016.0` 这样的时间字符串；`2016.0` 这样的纯数字会被拒绝。一旦映射了自行列，
每一行都必须填写数值，没有自行的源请填 `0`。不提供自行列时按原坐标处理；不提供
历元列时，所有行使用 `COORDINATE_EPOCH`（默认 `J2000`）。其他列名、时分秒坐标、
其他单位和 FITS 源表见[第5节](#输入列单位与自行)。

之后若修正了源表，下一条命令会读取新版本，把旧版本保存在
`WORK_ROOT/logs/catalogues/` 中，并重新查询所有源。

### 查询并制定计划

查询源表中所有源的观测：

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch query
~~~

该命令查询 CASDA（不需要登录），并输出汇总，包括源的数量及其查询状态。源表很大时
需要一段时间；如果中途中断，重新执行同一命令即可从中断处继续。结果位于
`WORK_ROOT/state/`：

| 文件 | 内容 |
| --- | --- |
| `input_catalogue.csv` | 程序读入的源表，含规范化名称和解析后的坐标。请先检查这个文件 |
| `crosscatalogue_sources.csv` | 每个源的查询状态（`available`、`no_observations`、`query_failed`） |
| `observation_progress.csv` | 每个已选的“源—观测”组合一行 |
| `queries/<Source_ID>/observations.csv` | 单个源的全部候选观测，以及每条被拒的原因 |

查询总是覆盖整张源表；处理哪些行在制定计划时选择。未设置 `BEGIN`/`END` 时，
下面的命令为整张表制定计划：

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch run
~~~

只处理部分行时，可在 `config.py` 中设置 `BEGIN` 和 `END`（行号按原表从1开始，
包含两端），也可以在命令行中给出，例如 `run --begin 1 --end 20`。命令行选项只对
当次命令有效，因此之后针对同一批工作的每条命令都要重复这些选项，或者用
`run --resume` 继续。
也可以用 `--source 源名` 按名称选择（可重复使用；名称见 `input_catalogue.csv` 的
`Source_Name` 列）。该命令不下载任何数据，会输出：

- `selected_datasets` 和 `datasets_needing_work`：范围内的观测数，以及其中尚需
  处理的观测数；
- `target_attempts`：将要处理的“源—观测”组合数；
- `selected_archive_bytes_for_work`：大致的下载量（字节）；
- `execution-plan.csv`（每个计划目标一行）和 `effective-settings.json`（全部
  有效设置及其来源）的路径，二者都在 `WORK_ROOT/state/runs/<运行ID>/` 中。

执行前请对照存储配额检查下载量。如果计划不符合预期，修改设置或选项后重新运行
即可。这份预览属于未完成的计划，因此命令会列出差异并询问是否替换，回答 `y` 即可。

**共享观测。** 如果源表中另一个源与计划中的源位于同一次观测（同一 SB、同一
beam），即使它不在所选行范围内，也会一并处理，因为耗时的步骤只需做一次。
但不会因此再加入该源的其他观测。

### 执行或恢复

在 `tmux` 中执行：

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch run --submit
~~~

控制程序会循环执行：在登录节点下载下一个作业所需的数据，提交一个 Slurm 作业，
每 60 秒检查一次（`POLL_SECONDS`），作业结束后准备下一个作业。每次检查都会输出
一行 `Job <id>: N datasets, S slots, H h, C CPUs`，全部完成后输出最终汇总。

按 Ctrl-C 可以停止控制程序，已提交的作业会继续运行。要继续执行，用相同的选项
再次运行同一条命令，或者执行

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch run --resume --submit
~~~

它会重复未完成计划记录的选项（并把这些选项显示出来）。`--resume` 只能与
`--submit`、`--prepare-download`、`--work-dir`、`--config`、`--dstools-config`
一起使用；不加 `--submit` 或 `--prepare-download` 时只显示计划。两种方式都会显示
`Continuing run …` 并重新接管这些作业；如果期间修改过设置文件，命令会先列出差异
并询问。你运行过的每条命令都记录在 `WORK_ROOT/logs/commands/` 各文件的 `command`
字段中。

### 更新观测索引

查询设置和源表都没有变化时，每条命令都会复用已保存的观测索引，不会访问 CASDA。
两者之一发生变化时，下一条 `query` 或 `run` 会发现这一点，并用当前设置重新查询
**所有**源；源表很大时需要一段时间。若设置都没变，只想查找上次查询后新发布的
观测，或只重查查询失败的源，执行以下之一：

~~~bash
# 重新查询所有源：查找新发布的观测
"$ASKAP_HOST_PYTHON" askap-batch query --update-query
# 只重查查询失败的源
"$ASKAP_HOST_PYTHON" askap-batch query --retry-query-failures
~~~

两者择一使用，不要同时使用。查询*失败*（例如网络错误）与查询成功但没有合格观测
是两回事，`crosscatalogue_sources.csv` 会区分这两种情况。新的查询结果会使未完成
的计划失效，因此存在未完成的计划时，`query` 会先询问是否替换；如果只想查看状态，
请使用 `progress`（第4节）。

> **查询筛选条件请写在 `config.py` 中。** 只作为 `query` 选项给出的筛选条件（例如
> `--exposure-min 300`）只对那一条命令有效。下一条 `run` 会把已保存的查询与
> `config.py` 比较，发现不同后，用 `config.py` 中的值重新查询所有源。请在
> `config.py` 中设置 `EXPOSURE_MIN`、`QUALITY_LEVELS`、`MINIMUM_SB`、`RA_UNIT` 等，
> 或者在每条命令上重复相同的 `--set` 选项。

## 3. 单个坐标处理

### 准备设置

单源命令读取同样的 `config.py` 和 `dstools-config.py`：`CONTAINER`、
`CASDA_USERNAME`、`PYTHON`、查询筛选条件、DStools 参数和资源设置都同样适用。源表设置（`CATALOGUE_*`）、`BEGIN`/`END` 和
`PRODUCT_ROOT` 会被忽略：坐标在 `query` 时给出，结果保存在该目标自己的工作目录中。
请始终用 `--work-dir` 为每个目标指定单独的目录；否则会使用 `config.py` 中批量
处理的 `WORK_ROOT`。

### 查询与查看观测表

~~~bash
"$ASKAP_HOST_PYTHON" askap-source query \
  --ra 180.1494 --dec=-28.6159 --name MySource \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource
~~~

命令会输出汇总（候选数、合格记录数、SB 数、默认选择、预计下载字节数）以及观测表
路径 `WORK_ROOT/query/observations.csv`；同一目录下的 `observations-columns.md`
解释了各列含义。表中每行是一个候选数据集，列出其 SB、beam、角距离、曝光时间、
质量、是否公开、是否合格（不合格时注明原因）以及是否被选中。默认每个 SB 选择
距离最近的合格 beam。`Record_ID` 是数据集的完整标识，供 `--observation-key` 使用。
直接修改这个 CSV 不会改变选择。

坐标的其他写法：

~~~bash
# 时分秒格式：赤经以小时为单位，赤纬以度为单位
"$ASKAP_HOST_PYTHON" askap-source query \
  --ra "12:00:35.856" --dec="-28:36:57.24" --ra-unit hourangle --name MySource \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource

# 带自行（mas/yr，--pmra 为 μα cos δ），参考历元 J2016.0
"$ASKAP_HOST_PYTHON" askap-source query \
  --ra 180.1494 --dec=-28.6159 --pmra -120.5 --pmdec 35.2 --coordinate-epoch J2016.0 \
  --name MySource --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource
~~~

负赤纬请用等号写法（`--dec=-28.6159`），以免负号被当成选项。省略 `--name` 时，
程序根据坐标生成名称，例如 `J120035.856-283657.24`。其他选项还有 `--pm-unit` 和
`--no-pmra-includes-cosdec`（见第5节）。

要修正坐标、名称、自行或历元，用修正后的值重新运行 `query`，之后的 `run` 就会
使用它们。之后也可以再次查询，以查找新观测。与批量处理一样，查询筛选条件请写在
`config.py` 中：下一条 `run` 会用 `config.py` 中的值重新查询。

### 制定计划与执行

~~~bash
# 查看计划
"$ASKAP_HOST_PYTHON" askap-source run \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource

# 执行；之后用同一条命令继续（在 tmux 中）
"$ASKAP_HOST_PYTHON" askap-source run \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource --submit
~~~

单源模式保留全部数据：下载的归档、原始测量集（MS）、处理副本、模型、完整图像和
截图，请预留数倍于下载量的空间。

### 首次计划的可选范围

若要处理某些 SB 的**全部**合格 beam，而不只是最近的一个，加上 `--all-beams-for-sb`：

~~~bash
"$ASKAP_HOST_PYTHON" askap-source run \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource \
  --all-beams-for-sb 35429 21677 --submit
~~~

列出的 SB（可带或不带 `SB` 前缀）会展开为其全部合格 beam；其他 SB 仍只处理最近
的合格 beam，所以这不是白名单。SB 不存在、没有合格 beam、或同一 beam 有两个不同
数据集时，会在下载前报错。之后针对这个目标的每条命令都要重复这个选项，或者用
`run --resume` 继续；省略它的命令会先询问，确认后才用只含最近 beam 的新计划取代
原计划。

两种模式都可以用 `--observation-key 标识`（可重复）把计划限制在指定数据集：
单源使用观测表中的 `Record_ID`，批量使用进度表中的 `Observation_Key`。这个选项
同样需要在之后的命令中重复（或使用 `--resume`）。

## 4. 进度、恢复与重新处理

### 查看状态

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch progress \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue
"$ASKAP_HOST_PYTHON" askap-batch failures \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue
~~~

单源请改用 `askap-source` 和对应的工作目录。这两个命令不会改动计划，控制程序运行
时也可以执行。`progress` 会刷新 `WORK_ROOT/state/` 中的表格，并输出各状态的数量
汇总。`failures` 列出失败的尝试（`env_failed`、`failed`、`exhausted`），其中包括
之后已被重试的旧记录，当前状态请以进度表为准。

主表是 `state/observation_progress.csv`，每个“源—观测”组合一行，各列说明见
`state/observation-progress-columns.md`。最常用的几列：

| 列 | 含义 |
| --- | --- |
| `Status` | `not_started`、`preparing`、`queued`、`running`、`partial`（在作业时限前被中止，将在下一个作业中继续）、`complete`、`env_failed`（因运行环境中止，下一条命令会自动重试）、`failed`（处理失败，只有 `--retry-failed` 才会重试）、`exhausted`（两次重试后仍然失败，只有 `--retry-exhausted` 才会重试）、`superseded`（所属计划已被取代） |
| `Failed_Step`、`Error`、`Log` | 最近一次尝试在哪一步、因何失败，以及应查看的日志文件 |
| `archive_download` … `cleanup` | 每个步骤一列：`Y` 完成，`N` 失败，`NA` 不需要，空白表示未执行 |
| `DS`、`I`、`V` | 结果文件的路径 |
| `Transfer_Status` | 结果拉取并校验后为 `verified`（见第6节） |

本流程的 Slurm 作业名为 `ASKAP-auto`：`squeue -u "$USER" --name=ASKAP-auto` 可列出
这些作业，作业输出在 `WORK_ROOT/logs/slurm-<作业号>.out` 和 `.err` 中。作业正常
结束并不代表每个目标都成功，请以 `Status` 为准。

### 断线恢复

如果控制程序已经停止（未使用 `tmux` 就退出了登录、按了 Ctrl-C，或登录节点重启），
先确认旧进程确实已经结束，例如用 `pgrep -u "$USER" -af askap-` 查看。然后按第1节
设置环境，用相同的选项再次运行同一条命令，或执行 `run --resume --submit`。它会接管
仍在 Slurm 中的作业，跳过已完成
的目标，继续原有计划。一个工作目录同一时间只能由一个控制程序使用，第二个会停止并
提示 “Another process owns …”。进程仍在运行时，不要删除它的锁文件。

### 修改设置

要修正某项设置、源表中的条目，或未完成工作的处理范围：

1. 停止控制程序（Ctrl-C）。启动新命令时它必须已经停止，否则新命令会停止并提示
   “Another process owns …”。
2. 检查作业：`squeue -u "$USER" --name=ASKAP-auto`。等待其结束；如果作业的设置
   有误，用 `scancel 作业号` 取消。
3. 修改 `config.py`、`dstools-config.py` 或源表，然后重新运行命令（或
   `run --resume`）。命令会列出与未完成计划的差异，例如：

   ~~~text
   This command differs from it in:
     ACCOUNT: None -> 'myproject'
     MODEL['robust']: 0.5 -> -0.5
   Replace the unfinished plan? [y/N]
   ~~~

   回答 `y` 即替换计划；其他任何回答（或不回答）都会保持原计划不变。

旧计划会作为历史保留，其中未完成的目标变为 `superseded`；如果新命令选中了它们，
会按新设置重新纳入计划。已完成的结果会保留，不会重新计算（见下文
`--rerun-completed`）。修改设置本身不会重试失败的目标：环境失败（`env_failed`）
在任何命令中都会重试，处理失败（`failed`）只有 `--retry-failed` 才会重试（见下一
小节）。上一份计划已经完成时，不需要替换，命令会直接运行而不询问。

### 失败重试与成功重算

~~~bash
# 重试处理失败（Status 为 failed）的目标
"$ASKAP_HOST_PYTHON" askap-batch run --retry-failed --submit

# 排除原因后，重试重试次数已用完（Status 为 exhausted）的目标
"$ASKAP_HOST_PYTHON" askap-batch run --retry-exhausted --submit

# 用当前设置重新计算某个源已完成的结果
"$ASKAP_HOST_PYTHON" askap-batch run --source Example_A --rerun-completed --submit
~~~

失败分为两类，显示在 `Status` 中：

| 类型 | 原因 | 何时重试 |
| --- | --- | --- |
| `env_failed`（环境失败） | CASDA 暂存或下载出错，且已尝试 `DOWNLOAD_RETRIES` 次（默认3次，间隔30秒）；磁盘空间或暂存预算不足；Slurm 作业未正常完成就结束（节点故障、被取消、内存不足、模块或镜像问题）；作业反复达到时限（`MAX_CONTINUATIONS`） | 下一条命令自动重试，即使命令和设置完全相同；不占用重试次数 |
| `failed`（处理失败） | DStools 某一步出错或没有产生输出；截图失败；有自行的源缺少观测时间；归档损坏或无法读取 | 只有 `--retry-failed` 才会重试 |

同一次控制程序运行中，环境失败不会被再次重试。空间不足，或者原因会在每个作业中
重复出现（镜像检查失败，或作业失败、被取消、内存不足）时，控制程序会停止，并给出
原因和 Slurm 日志位置；排除问题后重新运行命令即可。节点故障后，控制程序会继续处理
下一个作业。登录和凭据错误会使控制程序立即停止，不会把任何目标记为失败。

`--retry-failed` 会在**同一次命令中**给每个处理失败的目标最多再处理两次：

1. 复用已下载的数据，使用该目标私有的副本和模型重新处理；
2. 如果仍然失败，重新下载数据后再处理一次。

之后该目标变为 `exhausted`，普通运行和 `--retry-failed` 都不会再处理它。排除原因
（例如修改了某个 `MODEL` 选项）后，`--retry-exhausted` 会给所选范围内 `exhausted`
的目标重新进行一轮同样的两次尝试；两次都失败时，目标再次变为 `exhausted`。
`--retry-exhausted` 不处理其他任何目标：处理失败、被运行环境中止以及尚未处理的
目标都留给其他命令。

被运行环境中止的显式重试不计入次数：同一个重试选项会重新进行这一次重试。重试不在
源之间共享模型，计算量更大。所选范围（`BEGIN`/`END`、`--begin`/`--end`、
`--source`、`--observation-key`）会限制重试，但与所选目标共享观测、且属于同一类的
目标也会一并重试。要继续被中断的重试，请重复带同一个重试选项的命令（或使用
`run --resume`）；不带它的命令会先询问，确认后才替换重试计划。

> **重试前先排除原因。** 先运行 `failures`，查看每个处理失败的 `error`。否则两次
> 尝试可能因同样的原因失败，目标会变为 `exhausted`。

`--rerun-completed` 只对你选择的源或行，用新的、独立的尝试重新计算已完成的结果。
旧结果在新尝试成功之前一直可用。仅修改设置不会让已完成的结果重新计算，需要重算
时请使用这个选项。

单源模式支持同样的选项，并会保留处理失败的输入数据。

### 提交状态不明确

如果 `sbatch` 明确拒绝了作业（例如分区或账户无效），命令会停止并显示 Slurm 给出
的原因；在 `config.py` 中改正 `PARTITION` 或 `ACCOUNT` 后重新运行即可（询问是否
替换计划时回答 `y`）。

如果控制程序恰好在调用 `sbatch` 之后、记录结果之前停止，或者 `sbatch` 失败但没有
给出明确结果（例如超时），程序无法知道作业是否已经创建，因此不会重复提交。下一条
命令会停止，并在提示中给出本流程的作业标识（例如
`20261015T034649-99be0f87b0d3`，它不是 Slurm 的数字作业号）。先查看 Slurm：

~~~bash
squeue -u "$USER" --name=ASKAP-auto
sacct -X -u "$USER" --name=ASKAP-auto --starttime=now-2days
~~~

如果作业存在，关联它的 Slurm 作业号：

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch resolve-submission \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue \
  --job PIPELINE_JOB_ID --scheduler-id EXISTING_SLURM_ID
~~~

只有确定没有创建任何作业时，才把 `--scheduler-id EXISTING_SLURM_ID` 换成
`--confirmed-not-submitted`。之后重新运行你的命令。单源模式使用同样的命令，
把入口换成 `askap-source`。

## 5. 常用配置与参数

### 输入列、单位与自行

`CATALOGUE_COLUMNS` 告诉程序每个量在源表的哪一列；`None` 表示源表中没有这一列：

| 键 | 必需 | 内容 | 默认列名 |
| --- | --- | --- | --- |
| `source_name` | 是 | 源名称 | `Name` |
| `ra` | 是 | 赤经，ICRS | `RA` |
| `dec` | 是 | 赤纬，ICRS | `DEC` |
| `pmra` | 否 | 赤经方向自行 | `None` |
| `pmdec` | 否 | 赤纬方向自行 | `None` |
| `epoch` | 否 | 源表坐标对应的历元 | `None`（使用 `COORDINATE_EPOCH`） |

单位与格式：

| 设置 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `RA_UNIT` | `deg` 或 `hourangle` | `deg` | `hourangle` 接受 `12:00:35.856` 或 `12h00m35.856s` |
| `DEC_UNIT` | `deg` | `deg` | 十进制度或度分秒，例如 `-28:36:57.24` |
| `PM_UNIT` | Astropy 能识别的角速度单位，如 `mas/yr`、`arcsec/yr` | `mas/yr` | 同时作用于两个自行列 |
| `PMRA_INCLUDES_COSDEC` | `True` 或 `False` | `True` | `True`：`pmra` 为 μα cos δ（与 Gaia 相同）；`False`：未乘 cos δ 的 μα |
| `COORDINATE_EPOCH` | 时间字符串 | `J2000` | 没有历元列时，所有行都使用它 |
| `CATALOGUE_FORMAT` | `csv` 或 `fits` | `csv` | |
| `CATALOGUE_FITS_HDU` | HDU 编号 | `1` | 仅用于 FITS 源表 |

**历元**必须是 Astropy 能识别的时间字符串：儒略历元（`J2000`、`J2016.0`）、
贝塞尔历元（`B1950`）或 ISO 日期（`2016-01-01T00:00:00`，UTC）。`2016.0`（小数年）
或 `57388.5`（MJD）这样的纯数字会被直接拒绝，程序不会猜测其含义；请先转换，例如
`2016.0` → `J2016.0`，或把 MJD 换算成 ISO 日期。

**自行规则：**

- 没有映射的自行列按零自行处理。
- 已映射的自行列每一行都必须是数值。空白或非法值会使源表导入停止，并报告所在
  行号；自行未知或希望忽略的源请填 `0`。
- 自行从历元推算到每次观测的开始时间。观测时间优先取自 CASDA，CASDA 没有时
  从测量集读取。两者都无法取得时，有自行的目标会在 `proper_motion` 步骤失败；
  没有自行的源不受影响。

现成的带自行、时分秒坐标的示例是
[targets_sexagesimal.csv](../../examples/targets_sexagesimal.csv)，对应设置为：

~~~python
CATALOGUE_COLUMNS = {"source_name": "Object", "ra": "RightAscension", "dec": "Declination",
                     "pmra": "MuAlphaStar", "pmdec": "MuDelta", "epoch": "Epoch"}
RA_UNIT = "hourangle"
~~~

使用 FITS 源表时，设置 `CATALOGUE_FORMAT = "fits"`（表格不在 HDU 1 时再设置
`CATALOGUE_FITS_HDU`），列映射方法相同。[targets.fits](../../examples/targets.fits)
中的两个源与 `targets.csv` 相同。FITS 中被屏蔽（masked）的单元格按空白处理。

**源名称**会被规范化：连续空白变为一个 `_`。名称只能包含 ASCII 英文字母、数字和
`_ . + -`，须以字母或数字开头，不能包含 `..`，最长 120 个字符，且规范化后不能
重名。`--source` 和 `PRIMARY_BEAM_BY_SOURCE` 使用规范化后的名称（见
`state/input_catalogue.csv` 的 `Source_Name` 列）。源表不能包含以下保留列名：
`Processing_Index`、`Source_ID`、`Original_Source_Name`、`Source_Name`、`RA_deg`、
`Dec_deg`、`PMRA_cosdec_masyr`、`PMDEC_masyr`、`Epoch_MJD`、`Epoch_Scale`。

源表、列映射或单位改变后，下一条命令会读取新版本并重新查询所有源；旧版本保存在
`WORK_ROOT/logs/catalogues/` 中。

单源对应的选项是：`--ra-unit`、`--pmra`、`--pmdec`、`--pm-unit`、
`--pmra-includes-cosdec` / `--no-pmra-includes-cosdec` 和 `--coordinate-epoch`。

### 参数的提供方式

两个命令都按以下顺序读取设置，优先级从高到低：

1. 命令行选项；
2. `ASKAP_<设置名>` 环境变量；
3. `config.py` 和 `dstools-config.py`（或 `--config`、`--dstools-config` 指定的
   文件）。

程序没有内置默认值。如果某项设置在两个文件中都不存在，也没有通过其他方式给出，
命令会在执行任何操作之前停止并指出它，例如
`Settings missing (there are no built-in defaults): dstools-config.py: CROP_ARCMIN`。
在解决更新冲突时丢失了某行（见[安装](INSTALLATION.md)第2节）就可能出现这种情况；请从
仓库中的版本复制所列的行及其注释，例如用 `git show HEAD:dstools-config.py` 查看。写错文件的设置会被拒绝，提示中会指出正确的文件。

各处的写法：

- **`config.py`、`dstools-config.py`：** 大写设置名、Python 写法，例如
  `EXPOSURE_MIN = 300.0`、`EXCLUDED_SBS = [12345]`、`PRIMARY_BEAM = None`。
- **`--set`：** 小写设置名、JSON 写法，例如 `--set exposure_min=300`、
  `--set excluded_sbs='[12345]'`。`null` 表示关闭一项可选限制：
  `--set exposure_min=null`。
- **环境变量：** `ASKAP_` 加大写设置名，值用 JSON 写法，例如
  `export ASKAP_EXCLUDED_SBS='[12345]'`。

命令行选项只对当次命令有效，环境变量只在设置期间有效；需要长期使用的设置请写在
设置文件中。每个计划都会在 `effective-settings.json` 中记录各设置的有效值及来源，
每条命令也会连同两个文件的校验和记录在 `logs/commands/` 中。两个文件中的注释说明了
每项设置。

常用设置：

| 用途 | 设置 | 命令行 | 文件中的值 |
| --- | --- | --- | --- |
| 工作目录 | `WORK_ROOT` | `--work-dir` | — |
| DStools 镜像 | `CONTAINER` | `--container` | — |
| 最短曝光（严格大于） | `EXPOSURE_MIN` | `--exposure-min` | 500 秒 |
| 接受的数据质量 | `QUALITY_LEVELS` | `--set quality_levels='["GOOD"]'` | `GOOD`、`UNCERTAIN` |
| SB 限制 | `MINIMUM_SB`、`EXCLUDED_SBS` | `--minimum-sb` | 无 |
| 源表行范围（批量） | `BEGIN`、`END` | `--begin`、`--end` | 整张表 |
| 并行槽数 × 每槽线程 | `PARALLEL_SLOTS`、`THREADS_PER_SLOT` | `--parallel-slots`、`--threads-per-slot` | 4 × 8（最多 32 CPU） |
| 每个作业的内存 | `MEMORY_GB` | `--memory-gb` | 160 GiB |
| 作业时长上限 | `WALLTIME_HOURS` | `--walltime-hours` | 48 小时（上限，不是每个作业的申请值） |
| 截图大小 | `CROP_ARCMIN`（dstools-config.py） | `--crop-arcmin` | 10 角分 |
| 目标保护半径 | `MASK_RADIUS_ARCSEC`（dstools-config.py） | `--mask-radius-arcsec` | `None`：由 DStools 决定 |
| 天空模型选项 | `MODEL`（dstools-config.py） | `--model-option 选项名=值` | 见下文 |
| 其他设置 | （见两个文件） | `--set 键名=值` | 见两个文件 |

未注明文件的设置都在 `config.py` 中。

### DStools 参数（`dstools-config.py`）

`dstools-config.py` 保存 DStools 计算所用的参数：`MODEL`（`dstools-create-model`
的选项）、`MASK_RADIUS_ARCSEC`（`dstools-insert-model`）、`EXTRACT_MINUV_M`、
`EXTRACT_NOFLAG`、`EXTRACT_BASELINE_AVERAGE`、`PRIMARY_BEAM` 和
`PRIMARY_BEAM_BY_SOURCE`（`dstools-extract-ds`），以及 `CROP_ARCMIN`（截图）。

`MODEL` 列出了 `dstools-create-model` 的全部选项，以及实际传给 DStools 的值。这些
值都是 DStools 的默认值，只有四项为了重现我们原有流程的处理而不同：`band`
`AK_low`、`iterations` 100000、`multiscale` `True`、`minuvw-m` 200。每个值都会显式
传给 DStools，因此即使镜像中 DStools 的默认值不同，处理也保持一致；`None` 表示不传
这个选项（由 DStools 自行计算，例如 `imsize` 和 `cell`），`erode-beam-shape` 这类
开关为 `False` 时表示不加这个开关。要修改某个选项，直接改它的值，例如
`"robust": -0.5,`；只对一条命令生效时也可用 `--model-option robust=-0.5`。选项名是
`dstools-create-model` 的长选项名，去掉开头的 `--`。字典中的每个选项都要保留；未知
选项名会被拒绝，整数选项须填整数（写 `"threshold": 3`，不要写 `3.5`）。输出路径和
文件前缀由程序管理。

## 6. 在 Ada 拉取批量产物

在目标机器（我们使用 Ada）上，放置好脚本（见[安装](INSTALLATION.md)第6节）后执行：

~~~bash
ADA_PROJECT=/PATH/TO/ada/DStools-Auto-Processing

python3 "$ADA_PROJECT/bin/pull_products.py" \
  --user YOUR_OZSTAR_USER --host ozstar.swin.edu.au \
  --remote-work /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue \
  --destination "$ADA_PROJECT/catalogue" \
  --password-file ~/.config/askap/ozstar-transfer-password
~~~

使用 SSH 密钥或 agent 时省略 `--password-file`。`--remote-products` 默认为
`<remote-work>/products`，修改过 `PRODUCT_ROOT` 时请指定。每个批量工作目录使用
单独的目标目录。默认拉取全部已完成的结果，`--source 源名`（可重复）可限制范围。

脚本会列出 OzSTAR 上已完成的结果及其 SHA256 校验和，只复制缺失或不一致的文件，
逐一校验复制结果，确认传输期间 OzSTAR 上的结果没有变化，然后向工作目录上传一份
回执。再次运行时只复制新增的内容。脚本不会删除任何一端的数据。

目标目录中的内容：

~~~text
DESTINATION/
├── <Source_Name>/<数据集目录>      → 指向 .versions/ 中当前结果的链接
│     <SB>_<beam>.ds                           动态谱
│     wsclean_model/wsclean-MFS-I-image.fits    Stokes I 截图
│     wsclean_model/wsclean-MFS-V-image.fits    Stokes V 截图
├── .versions/                      实际文件；请保留这个隐藏目录
└── state/                          台账、清单和回执，记录每个结果的来源
~~~

这两个 FITS 文件是截图（边长 `CROP_ARCMIN`），只是沿用了 WSClean 全场图像的文件名。
每个结果的来源信息（SB、beam、尝试、设置版本、镜像校验和）记录在 `state/` 下的
清单和回执中。

之后在 OzSTAR 上运行 `progress`：它会导入回执，并把相应结果的 `Transfer_Status`
标为 `verified`。

也可以从设置文件读取连接信息：把你的 `config.py` 复制到目标机器，运行
`python3 pull_products.py --config /PATH/TO/config.py`。脚本会使用其中的
`OZSTAR_USER`、`WORK_ROOT`、`ADA_ROOT`（目标目录），以及已设置的 `PRODUCT_ROOT`、
`OZSTAR_HOST`、`TRANSFER_PASSWORD_FILE`。命令行选项优先于 `ASKAP_*` 环境变量，
环境变量又优先于设置文件。这类副本和密码文件请只保存在自己的机器上，不要放进仓库。

此脚本不处理单源结果。单源需要的文件请依据 `state/observation_progress.csv` 中的
路径自行复制（例如使用 `rsync`）。
