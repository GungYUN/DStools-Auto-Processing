# 4. SIF 构建（可选）

[索引](../../README.md) · [English](../en/SIF_BUILD.md) · [安装](INSTALLATION.md)

只有在没有合适的 DStools 镜像时才需要本页。如果课题组或维护者已提供镜像，直接
使用即可，可以跳过本页。只要本项目用到的 DStools 命令和选项保持兼容，更新本项目
的程序就不需要更换镜像。

镜像必须提供 DStools 命令、位于 `/opt/wsclean/bin/wsclean` 的 WSClean，以及位于
`/opt/askap/bin/python`、带 casacore 和 Astropy 的 Python（见[安装](INSTALLATION.md)
第3节）。下面的配方正是用来构建这样的镜像：所有输入都固定版本并校验哈希，构建在
计算节点上完全离线进行。我们的结果使用按此配方构建的镜像得到。

## 1. 构建配方

配方位于 [New_dstools_build](../../New_dstools_build/)：

| 文件 | 用途 |
| --- | --- |
| `versions.env` | 固定全部源码与依赖的版本、宿主模块及构建目录 |
| `prep_sources.sh` | 在登录节点运行：下载并固定全部构建输入 |
| `askap-dstools.def` | 离线 Apptainer 定义文件 |
| `build_sif.sbatch` | 在计算节点运行：编译全部组件并组装镜像 |
| `patches/` | 构建时应用的源码补丁；请上传整个文件夹 |

这些补丁会影响处理结果，请了解它们的作用：

| 补丁 | 作用 |
| --- | --- |
| `0001-visibility-weighting-unit` | DStools 成像时以 `-visibility-weighting-mode unit` 调用 WSClean |
| `0002-unique-integrations` | DStools 按不同时间戳的个数统计积分数 |
| `0003`、`0004`、`0005` | WSClean 构建修正（Boost、离线依赖、一致的 OpenSSL 库），不影响结果 |
| `0006-dstools-subprocess-drain-and-check` | DStools 等待每个外部命令运行结束，命令失败时报错停止 |

用其他来源构建、且没有等效修改的镜像，可能给出不同的结果，也可能在内部步骤失败时
仍报告成功。

使用配方前，先在自己的电脑上修改以下占位值：

| 位置 | 设置内容 |
| --- | --- |
| `versions.env` 中的 `BUILD_ROOT` | 一个新的空构建目录，例如 `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/dstools/sif-build-new` |
| `build_sif.sbatch` 中的 `BUILD_ROOT=` 以及 `#SBATCH --output`、`--error` 路径 | 同一个目录，使两个文件一致 |
| `versions.env` 中的 `CONTROLLER_PYTHON_BIN` | 你的宿主 Python（装有本项目依赖的 Python 3.12）；其中 `astroquery`、`keyring` 的版本会被写入镜像 |
| `versions.env` 中的 `HOST_*_MODULE` | 站点模块名与 OzSTAR 不同时修改 |

这些设置只用于新的构建，不会改变正在使用的镜像。

配方不会下载 DStools 本身。请向维护者获取指定的 DStools 源码快照：配方会用
`versions.env` 中记录的文件哈希核对快照，其他版本会被拒绝。不要把更新的 DStools
源码放进这个固定版本的配方。源码、补丁或依赖改变时，请使用新的构建目录，并保留
之前的镜像及其清单。

## 2. 上传完整配方和源码

修改完占位值后，在自己的电脑上执行，并把示例目录换成你自己的新 `BUILD_ROOT`：

~~~bash
BUILD_ROOT=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/dstools/sif-build-new
ssh YOUR_OZSTAR_USER@ozstar.swin.edu.au \
  "mkdir -p '$BUILD_ROOT/recipe' '$BUILD_ROOT/src/dstools-dev' '$BUILD_ROOT/logs'"
rsync -av New_dstools_build/ "YOUR_OZSTAR_USER@ozstar.swin.edu.au:$BUILD_ROOT/recipe/"
rsync -av /path/to/exact/dstools-dev/ \
  "YOUR_OZSTAR_USER@ozstar.swin.edu.au:$BUILD_ROOT/src/dstools-dev/"
~~~

`/path/to/exact/dstools-dev/` 是 DStools 快照在你本地的路径。配方就是本仓库中的
`New_dstools_build/` 目录；仓库已克隆在 OzSTAR 上时，也可以不用第一条 `rsync`，
直接在那里复制：`cp -r New_dstools_build/. "$BUILD_ROOT/recipe/"`。

## 3. 登录节点准备，计算节点构建

在 OzSTAR 登录节点逐条执行，前一条成功后再执行下一条：

~~~bash
BUILD_ROOT=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/dstools/sif-build-new
cd "$BUILD_ROOT/recipe"
bash prep_sources.sh
sha256sum -c --quiet "$BUILD_ROOT/manifests/build-input-files.sha256"
sbatch build_sif.sbatch
~~~

`prep_sources.sh` 需要联网：它下载全部输入、应用并检查补丁，并写出输入清单。随后
的 Slurm 作业在完全不联网的情况下用 `apptainer build --fakeroot` 构建镜像（不需要
root 账号，但站点必须允许 fakeroot 构建）。该作业申请 8 CPU、32 GB 内存、32 GB 本地
磁盘和 12 小时。

输入、缓存、日志、清单和镜像都保存在 `BUILD_ROOT` 下。准备完成后若修改了配方，
请重新运行准备步骤；不要为绕过不一致而手工修改记录的哈希。构建失败时，查看
`BUILD_ROOT/logs/build-<作业号>.err`。

## 4. 保存和采用新镜像

构建成功后，候选镜像位于 `BUILD_ROOT/artifacts/`，其路径和 SHA256 记录在
`manifests/candidate-sif.sha256` 中，构建日志在 `logs/` 中。作业结束前会以普通
用户身份运行几项简短测试（能否读取 casacore、Astropy 和 CASA 数据，子命令失败能否
被报告），但这些测试不能证明科学结果正确。

确认镜像能运行所需命令、并能正确处理真实数据后，用独立的文件名保存它，在
`config.py` 中把 `CONTAINER` 设为它的路径，然后重新运行命令；未完成的工作随后会
使用新镜像。使用某个镜像的作业仍在排队或运行时，不要覆盖该镜像文件：每个作业开始
前都会检查镜像的校验和，不一致时会停止并提示
“SIF changed after this run was prepared”。
