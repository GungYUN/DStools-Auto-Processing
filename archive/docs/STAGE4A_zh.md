# Stage4A：现有结果、成功跳过与真实MS时间回退

前提：Stage3A single及Stage3B shared3均已PASS，原工作区不再有活动任务。
脚本为 tests/validate_results_stage4a.py；不修改生产核心、科学参数或完成判定。

## 检查范围

- 只读报告/SQLite：成功指针、步骤、产物身份与大小、独立清理状态。
- Anchor_A的输入/自行/历元、SIF、生效科学参数、观测时间、修正坐标及自行复算。
- 通过SQLite backup和查询元数据建立副本，调用正式askap-source/askap-batch；
  限定原数据集且不加submit/prepare-download，要求选中1个数据集但工作量为0，
  没有新增attempt/jobs，原成功指针/步骤不变。不复制MS或大模型。
- 原SIF中核对两种模式共12件产物相对各自Stage3 SHA未变；分块比较Anchor的DS
  数值/坐标轴，以及I/V cutout数值/WCS/物理元数据。宿主无需新增h5py依赖。
- 调用正式Executor.observation_time：有效CASDA优先；仅在内存副本删除t_min，
  经真实Apptainer/SIF读取原始MS的TIME单位/尺度和UTC MJD，继续自行传播。
- 原作业数字Slurm ID及accounting：终态、退出码、Elapsed、CPU/内存、.batch MaxRSS。
- 最后复核原SQLite逻辑内容和JSON/CSV/ECSV指纹不变。科学输入只读bind；副本、
  日志和报告写入独立Stage4A目录。共11个顶层检查，产物比较内有4个子项。

这属于一次验收比较，不向主程序添加DS/FITS内容验证。

## 判定

- PASS，退出0：检查全通过，所比数组和物理元数据一致。文件封装不同但数值相同
  可以通过，不要求不同运行的文件SHA互相相同。
- REVIEW，退出2：数组数值差异、清理WARNING、资源资料缺项，或MS时间落在保存的
  CASDA区间外。提供最大绝对差值/RMS/相对L2/非有限值分布和元数据差异；
  不用统一容差自动放行。MS采样中心与CASDA边界也不要求逐位相同。
- FAIL，退出1：原文件指纹改变、身份/坐标/参数不一致、跳过行为错误、轴/WCS不同、
  真实MS时间不可读，或原Stage3状态改变。

REVIEW退出2可能使本次检查作业显示FAILED，应查看检查报告区分原因，不重跑Stage3。
结果一致不等于独立完成了全部天体物理质量验收。

## 1. 本地上传

~~~bash
cd /PATH/TO/DStools_Auto_Processing
./deploy.sh YOUR_OZSTAR_USER@ozstar.swin.edu.au /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
~~~

无需编辑远端config.py或原工作区配置。

## 2. Ozstar提交

1 CPU、4GiB、20分钟上限是检查资源，不改变冻结科学参数。无需下载或CASDA凭据；
提交后不需要保持登录controller运行。每次自动创建独立run目录，不覆盖旧报告。

~~~bash
ASKAP_STAGE4A_ROOT=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage4a
mkdir -p "$ASKAP_STAGE4A_ROOT"

cat > "$ASKAP_STAGE4A_ROOT/run_stage4a.sbatch" <<'BASH'
#!/bin/bash
#SBATCH --job-name=ASKAP-stage4A
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=00:20:00
set -Eeuo pipefail
module load gcc/13.3.0 python/3.12.3 apptainer
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
cd /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
exec /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/env/askap-python/bin/python -u tests/validate_results_stage4a.py \
  --single-work /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage3/single_SB74372_beam28 \
  --shared-work /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage3/shared3_SB74372_beam28 \
  --output-root /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage4a
BASH

set -o pipefail
ASKAP_STAGE4A_JOB=$(sbatch --parsable \
  --output="$ASKAP_STAGE4A_ROOT/slurm-%j.out" \
  --error="$ASKAP_STAGE4A_ROOT/slurm-%j.err" \
  "$ASKAP_STAGE4A_ROOT/run_stage4a.sbatch" | cut -d ';' -f 1)
printf 'Stage4A Slurm job: %s\n' "$ASKAP_STAGE4A_JOB"
~~~

## 3. 结果

~~~bash
squeue -j "$ASKAP_STAGE4A_JOB"
~~~

作业结束后：

~~~bash
cat "$ASKAP_STAGE4A_ROOT/slurm-$ASKAP_STAGE4A_JOB.out"
cat "$ASKAP_STAGE4A_ROOT/slurm-$ASKAP_STAGE4A_JOB.err"
~~~

末尾打印 STAGE4A PASS/REVIEW/FAIL 和本次 run-*/report.json 绝对路径。
细节日志：comparison.log、ms-time.log、skip-single.log、skip-shared3.log、sacct-*.txt。
若REVIEW/FAIL，请提供对应report.json与日志，不先修改科学参数或重算。

本地新增回归覆盖真实CLI跳过、原状态不变、HDF5封装差异、数值差异、指纹篡改、
WCS偏移、复杂NaN掩码、过期指针、时间回退分支、accounting缺项和完整驱动。
SIF/MS/Slurm边界在离线回归中为模拟器，不能代替本次现场通过。
本地样本依赖h5py；未安装时这组回归明确skip，现场脚本仍使用SIF的h5py。
