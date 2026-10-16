# 1. 安装

[索引](../../README.md) · [English](../en/INSTALLATION.md) · [使用](USAGE.md)

本页只需完成一次：把程序放到 OzSTAR、建立 Python 环境、保存 CASDA 密码，
并在目标机器上放置拉取脚本。完成后请继续阅读[使用与参数](USAGE.md)。

`YOUR_OZSTAR_USER` 等大写值以及以 `/PATH/TO/` 开头的路径都是占位值，使用时请换成
自己的值（见 [README](../../README.md) 中的表格）。模块名 `gcc/13.3.0`、
`python/3.12.3`、`apptainer` 是 OzSTAR 上的名称，其他站点请换成相应模块。

## 1. 环境要求

| 位置 | 在这里运行什么 | 需要什么 |
| --- | --- | --- |
| OzSTAR 登录节点 | 查询源表、从 CASDA 下载、提交并跟踪 Slurm 作业 | Python ≥3.10 及 `requirements.txt` 中的包；能访问 CASDA |
| OzSTAR 计算节点 | 在 Apptainer 中运行 DStools | DStools 镜像（`.sif`）；不需要联网 |
| 目标机器（我们使用 Ada） | 用 `pull_products.py` 拉取批量结果 | Python ≥3.7、`ssh`、`rsync`；仅在使用密码文件登录时需要 `sshpass` |

需要的账号：

- **OzSTAR 账号**，所在项目要有 Slurm 机时和足够的 `/fred` 存储空间；
- **CASDA 账号**（即 OPAL 登录），仅在下载数据时使用；
- 目标机器到 OzSTAR 的 **ssh 访问权限**。

计算节点按离线环境使用：凡是需要联网的操作（安装 Python 包、查询 CASDA、下载
数据）都在登录节点完成。拉取脚本只使用 Python 标准库。

## 2. 获取程序

直接在 OzSTAR 上克隆本仓库。程序目录应放在计算节点也能读取的位置（例如 `/fred`
下），因为 Slurm 作业会从这里运行程序。你的设置保存在该目录中的 `config.py` 和
`dstools-config.py` 里（见[使用与参数](USAGE.md)第2节）。

在登录节点执行（替换占位值）：

~~~bash
mkdir -p /fred/YOUR_PROJECT/YOUR_OZSTAR_USER
cd /fred/YOUR_PROJECT/YOUR_OZSTAR_USER
git clone https://github.com/GungYUN/DStools-Auto-Processing.git
cd DStools-Auto-Processing
~~~

以后更新时，在该目录执行 `git pull`。由于你会直接修改 `config.py` 和
`dstools-config.py`，当它们有本地修改时，Git 可能拒绝更新。可以先把修改暂存起来，
更新后再恢复，并检查这两个文件是否有冲突：

~~~bash
git stash
git pull
git stash pop
~~~

更新可能增加新的设置项：`git pull` 把它们带进这两个文件，`git stash pop` 再恢复你
自己填写的值。如果需要在设置
文件中解决冲突，请保留新增的行：缺少设置时，每条命令都会停止并提示
“Settings missing …”及缺少的设置名；用 `git show HEAD:config.py`（或
`HEAD:dstools-config.py`）可以查看仓库中的版本，从中复制。不要在该目录启动的处理
仍在运行时更新程序：正在运行的 Slurm 作业会使用这个程序目录。

## 3. 宿主 Python 与 SIF

### Python 环境

“宿主 Python”是 OzSTAR 上运行本项目命令以及各 Slurm 作业中 worker 的 Python。
DStools 在 SIF 镜像中使用镜像自带的 Python，两者互不影响。

在登录节点建立一次环境，位置要能被计算节点读取：

~~~bash
module load gcc/13.3.0 python/3.12.3 apptainer
cd /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools-Auto-Processing
python -m venv /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/askap-python
/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/askap-python/bin/python -m pip install -r requirements.txt
~~~

说明文档通过 shell 变量 `ASKAP_HOST_PYTHON` 指代这个解释器，每次登录时设置一次
（见[使用与参数](USAGE.md)第1节）。检查安装：

~~~bash
ASKAP_HOST_PYTHON=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/askap-python/bin/python
"$ASKAP_HOST_PYTHON" -m pip check
"$ASKAP_HOST_PYTHON" askap-batch --help
"$ASKAP_HOST_PYTHON" askap-source --help
~~~

Slurm 作业使用启动命令的那个解释器。若要改用其他解释器，在 `config.py` 中设置
`PYTHON`，两个命令都会使用它。已有装好这些包的 Python ≥3.10 环境时，也可以直接
使用。

### DStools 镜像（SIF）

处理需要一个包含 DStools 和 WSClean 的 Apptainer 镜像。可以使用课题组或维护者
提供的镜像，也可以按 [SIF 构建](SIF_BUILD.md)自行构建。镜像路径之后填写在
`config.py` 的 `CONTAINER` 中。只要本项目用到的 DStools 命令和选项保持兼容，
更新本项目的程序就不需要更换镜像。

镜像必须提供：

- 命令 `dstools-askap-preprocess`、`dstools-create-model`、`dstools-insert-model`、
  `dstools-subtract-model` 和 `dstools-extract-ds`；
- 位于 `/opt/wsclean/bin/wsclean` 的 WSClean；
- 位于 `/opt/askap/bin/python`、带 casacore 和 Astropy 的 Python
  （对应设置 `CONTAINER_PYTHON`）。

每个计划的第一个作业会检查镜像是否接受计划用到的全部命令行选项，但不会检查
镜像的科学处理行为。我们的结果使用 `New_dstools_build/` 配方构建的镜像得到，
该配方会给 DStools 打上影响处理结果的补丁（见 [SIF 构建](SIF_BUILD.md)第1节）。
用其他方式构建的镜像可能给出不同结果。

## 4. 目录与输入

建议把程序、处理数据和输入源表分开存放，例如：

| 内容 | 示例 |
| --- | --- |
| 程序（含你的 `config.py` 和 `dstools-config.py`） | `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools-Auto-Processing` |
| 批量工作目录 | `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue` |
| 单源工作目录 | `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource` |
| 输入源表 | `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/catalogues/targets.csv` |

*工作目录*保存一张源表或一个目标产生的全部内容：下载的归档、测量集（MS）、模型、
状态记录、日志和结果。使用规则：

- 每张源表（批量）或每个目标（单源）各用一个工作目录，批量和单源不要混用同一目录。
- 可以在同一工作目录中修正错误：修改源表或设置文件后重新运行命令即可；如果与
  未完成的计划不同，命令会先询问是否替换。源表的旧版本保存在 `logs/catalogues/`，
  每条命令记录在 `logs/commands/`。
- 如果用过早期的超冷矮星（UCD）处理流程，不要复用它的工作目录。
- 路径使用不含空格的绝对路径，只包含英文字母、数字和 `_`、`.`、`+`、`-`：
  DStools 内部会调用 shell 命令，无法处理其他字符。
- 单个下载归档可能有几十 GB（默认大小上限为 50 GiB），默认暂存预算为 2 TiB。
  处理大量观测前，请先确认 `/fred` 配额。

源表只要求每个源有名称、赤经和赤纬。可选的自行与历元列见
[使用与参数](USAGE.md)第2节。

## 5. CASDA 凭据

查询 CASDA 不需要账号，下载数据才需要。这里使用 CASDA（OPAL）账号，与 OzSTAR
登录密码无关。把用户名填写在 `config.py` 的 `CASDA_USERNAME` 中，两个命令都会使用。

密码不写在 `config.py` 中，程序在处理过程中也不会提示输入密码，因为下载是在无人值守
的情况下进行的。请用下面的命令一次性保存密码（详见[使用与参数](USAGE.md)第1节）。
该命令以不显示的方式请你输入密码，把它保存在只有你本人可读的文件中，并测试能否登录：

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch set-casda-password
~~~

密码只在登录节点读取，不会写入设置记录、命令日志或 Slurm 脚本。

## 6. 在 Ada 放置拉取脚本

批量结果用 `pull_products.py` 拉取到目标机器（我们使用 Ada）。任何能用 `ssh`
登录 OzSTAR 的 Linux 或 macOS 机器都可以。在该机器上执行：

~~~bash
ADA_PROJECT=/PATH/TO/ada/DStools-Auto-Processing
mkdir -p "$ADA_PROJECT/bin"
scp -4 YOUR_OZSTAR_USER@ozstar.swin.edu.au:/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools-Auto-Processing/pull_products.py \
  "$ADA_PROJECT/bin/"
python3 "$ADA_PROJECT/bin/pull_products.py" --help
~~~

（`-4` 表示强制使用 IPv4；网络不需要时可以去掉。）

脚本通过 `ssh` 和 `rsync` 登录 OzSTAR，建议使用 SSH 密钥或 agent。若必须使用
OzSTAR 密码，请用第5节的隐藏输入方法把它另存为一个文件（例如
`~/.config/askap/ozstar-transfer-password`，权限 600；内容是 OzSTAR 登录密码，
不是 CASDA 密码），并通过 `--password-file` 指定；这种方式还需要安装 `sshpass`。

下一步请阅读[使用与参数](USAGE.md)，选择批量或单源流程。
