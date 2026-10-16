# DStools-Auto-Processing

DStools-Auto-Processing finds public ASKAP observations of your targets in
CASDA, downloads the visibility data and runs DStools on the OzSTAR
supercomputer. For every target and observation it produces a **dynamic
spectrum (DS)** and **Stokes I and V image cutouts**. A companion script then
copies the finished results, with checksum verification, to another machine
(Ada in our setup).

There are two commands; both use the same processing code and the same two
settings files, `config.py` and `dstools-config.py`:

- **`askap-batch`** processes a source catalogue (CSV or FITS). Sources that
  lie in the same observation share one download and one sky model.
- **`askap-source`** processes one sky position. You first see a table of
  candidate observations, then process the selection; all intermediate data
  are kept for inspection.

## English documentation

**Before you start**, you need:

- an OzSTAR account and project — processing runs there through Slurm;
- a CASDA account (OPAL login) — needed to download data, not to search; you
  store the password once with `askap-batch set-casda-password`, not in
  `config.py`;
- a DStools Apptainer image (`.sif`) — from your group, or built with guide 4;
- for retrieval, a machine with Python ≥3.7, `ssh` and `rsync` that can log in
  to OzSTAR.

**Guides** — read them in this order:

1. [Installation](docs/en/INSTALLATION.md) — get the code, set up Python on
   OzSTAR, store your CASDA password and install the retrieval script.
2. [Usage and parameters](docs/en/USAGE.md) — step-by-step batch and
   single-source runs, progress, changing settings, retries and retrieval.
3. [How it works](docs/en/REFERENCE.md) — how observations are chosen and
   processed, how jobs are sized, and which files are kept where.
4. [SIF build (optional)](docs/en/SIF_BUILD.md) — build a DStools image only if
   you do not already have a suitable one.

**A typical batch run:**

1. Install once ([Installation](docs/en/INSTALLATION.md)).
2. Store your CASDA password once with `askap-batch set-casda-password`
   ([Usage §1](docs/en/USAGE.md#store-your-casda-password-once)), and edit
   `config.py` in the code directory: set your work directory, catalogue, image
   and CASDA login ([Usage §2](docs/en/USAGE.md#2-batch-catalogue-processing)).
3. Run `askap-batch query` to find observations, then `askap-batch run --submit`
   inside `tmux` to download and process them.
4. Follow the run with `askap-batch progress`.
5. Copy and verify the results with `pull_products.py` on your destination
   machine.

The processing commands run on an OzSTAR login node; `pull_products.py` runs on
the destination machine. Get the code with:

~~~bash
git clone https://github.com/GungYUN/DStools-Auto-Processing.git
cd DStools-Auto-Processing
~~~

The guides use these placeholders; replace them with your own values:

| Placeholder | Replace with |
| --- | --- |
| `YOUR_OZSTAR_USER` | your OzSTAR user name |
| `YOUR_PROJECT` | your OzSTAR project, i.e. your directory under `/fred` |
| `YOUR_CASDA_USERNAME` | your CASDA (OPAL) login |
| `/PATH/TO/...` | an absolute path of your choice |

Both commands read `config.py` (data, query, Slurm and account settings) and
`dstools-config.py` (DStools options) every time they start, so you edit them
directly and simply run the command again after a change. If an unfinished plan
would change, the command lists the differences and asks before replacing it.
Each command, with the settings it used, is recorded in the work directory
under `logs/commands/`.

## 中文说明

本项目在 CASDA 中查找目标的公开 ASKAP 观测，下载可见度数据，并在 OzSTAR
超算上运行 DStools。对每个目标的每次观测，生成一个**动态谱（DS）**和
**Stokes I、V 图像截图**；随后由配套脚本把结果复制到另一台机器（我们使用 Ada），
并逐文件校验。

两个命令共用同一套处理程序和同样两个设置文件 `config.py`、`dstools-config.py`：

- **`askap-batch`**：处理一张源表（CSV 或 FITS）。落在同一次观测中的多个源
  共用一次下载和一个天空模型。
- **`askap-source`**：处理单个天区坐标。先查看候选观测表，再处理所选观测；
  全部中间数据都会保留，便于检查。

**开始前需要准备：**

- OzSTAR 账号和项目——处理通过 Slurm 在 OzSTAR 上运行；
- CASDA 账号（OPAL 登录）——下载数据时需要，查询观测不需要；密码用
  `askap-batch set-casda-password` 一次性保存，不写入 `config.py`；
- DStools 的 Apptainer 镜像（`.sif`）——向课题组获取，或按第4篇说明自行构建；
- 拉取结果时，一台装有 Python ≥3.7、`ssh` 和 `rsync`、能登录 OzSTAR 的机器。

**说明文档**（建议按顺序阅读）：

1. [安装](docs/zh-CN/INSTALLATION.md)——获取程序、在 OzSTAR 配置 Python、保存
   CASDA 密码、放置拉取脚本。
2. [使用与参数](docs/zh-CN/USAGE.md)——批量与单源的逐步操作，以及进度、修改设置、
   重试和结果拉取。
3. [运行机制](docs/zh-CN/REFERENCE.md)——如何选择和处理观测、作业资源如何估算、
   各类文件保存在哪里。
4. [SIF 构建（可选）](docs/zh-CN/SIF_BUILD.md)——只有在没有合适镜像时才需要。

**一次典型的批量处理：**

1. 完成一次性安装（[安装](docs/zh-CN/INSTALLATION.md)）。
2. 用 `askap-batch set-casda-password` 一次性保存 CASDA 密码（[使用与参数](docs/zh-CN/USAGE.md)
   第1节），并编辑程序目录中的 `config.py`，填写工作目录、源表、镜像和 CASDA 登录名
   （同一文档第2节）。
3. 运行 `askap-batch query` 查找观测，再在 `tmux` 中运行
   `askap-batch run --submit` 下载并处理。
4. 用 `askap-batch progress` 查看进度。
5. 在目标机器上用 `pull_products.py` 拉取并校验结果。

处理命令在 OzSTAR 登录节点运行，`pull_products.py` 在目标机器运行。获取程序：

~~~bash
git clone https://github.com/GungYUN/DStools-Auto-Processing.git
cd DStools-Auto-Processing
~~~

说明文档中的占位值需替换为自己的值：

| 占位值 | 替换为 |
| --- | --- |
| `YOUR_OZSTAR_USER` | 你的 OzSTAR 用户名 |
| `YOUR_PROJECT` | 你的 OzSTAR 项目，即 `/fred` 下的项目目录 |
| `YOUR_CASDA_USERNAME` | 你的 CASDA（OPAL）登录名 |
| `/PATH/TO/...` | 自己选定的绝对路径 |

两个命令每次启动都会读取 `config.py`（数据、查询、Slurm 与账号设置）和
`dstools-config.py`（DStools 参数），因此直接编辑这两个文件即可；修改后重新运行
命令就会使用新的设置。若会改变尚未完成的计划，命令会先列出差异并询问是否替换。
每条命令及其使用的设置都会记录在工作目录的 `logs/commands/` 中。

## Repository layout / 目录说明

| Path / 路径 | Purpose / 用途 |
| --- | --- |
| `askap-batch`, `askap-source` | The two commands you run / 日常运行的两个命令 |
| `askap_auto/` | Shared processing code / 共用的处理程序 |
| `config.py` | Data, query, Slurm and account settings for both commands; edit it directly / 两个命令共用的数据、查询、Slurm 与账号设置，直接编辑 |
| `dstools-config.py` | DStools options and cutout size for both commands; edit it directly / 两个命令共用的 DStools 参数与截图尺寸，直接编辑 |
| `examples/` | Example catalogues / 源表示例 |
| `requirements.txt` | Python packages for OzSTAR / OzSTAR 上需要安装的 Python 包 |
| `pull_products.py` | Copies and verifies batch results on the destination machine / 在目标机器拉取并校验批量结果 |
| `docs/en/`, `docs/zh-CN/` | User guides / 使用说明 |
| `New_dstools_build/` | Optional recipe for building a DStools image / 可选的 DStools 镜像构建配方 |
| `archive/` | Development records; regression tests in `archive/tests/` / 开发记录，回归测试位于 `archive/tests/` |

Keep the code and the processing data in separate directories. Never commit
passwords, measurement sets, SIF images, results or runtime state, and do not
commit or push a `config.py` that contains your own paths and account.

程序目录与处理数据目录应分开存放。密码、测量集（MS）、SIF 镜像、处理结果和运行
状态都不要提交到仓库；填有个人路径和账户的 `config.py` 也不要提交或推送。
