# 第一阶段：离线输入表格适配

阶段顺序：1表格读取 → 2真实查询/执行计划 → 3最小真实单源与共享处理 →
4恢复、传输及扩大运行。本页仅执行第一阶段。

样本在 examples/validation/，每份4行，由人工构造，覆盖名称字符串、列映射、
度与时分秒、正负赤纬、RA边界、可选自行和参考历元。没有UCS或光谱型字段。
这些位置和自行不作为后续科学验收数据。

## 1. 在本地更新上传

```bash
cd /PATH/TO/DStools_Auto_Processing
./deploy.sh YOUR_OZSTAR_USER@ozstar.swin.edu.au /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
```

新CSV及检查脚本随examples/tests上传。若远端还有其他正在使用本项目代码的
真实作业，待其结束后再更新。部署仍保留远端config.py。

## 2. 在Ozstar登录节点运行

```bash
cd /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
module load gcc/13.3.0 python/3.12.3 apptainer
ASKAP_HOST_PYTHON=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/env/askap-python/bin/python
set -o pipefail
"$ASKAP_HOST_PYTHON" -u tests/validate_catalogue_stage1.py \
  --output-root /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage1 \
  2>&1 | tee stage1_catalogue_validation.log
```

无需修改config.py或准备CASDA密码/SIF；不需要申请计算节点。脚本直接调用
`catalogue.load_catalogue()`，不运行query、下载或Slurm提交。

## 3. 判断结果

每项显示PASS。末尾应显示 `STAGE1 PASS` 和报告的绝对路径；缺列、名称冲突、
非法坐标、空白已映射自行和冻结输入变化的案例，被正确拒绝才算PASS。
出现FAIL时保留输出，先解决这一项再进入第二阶段。

每次执行会在指定根目录创建独立run目录，内容包括：

- inputs：本次CSV副本、生成的FITS与其他测试输入。
- 各有效profile的state/input_catalogue.csv与.json：标准化结果及原始输入快照。
- catalogue-settings.json：各profile使用的列映射、单位和历元。
- report.json：实际检查结果、错误详情、解释器路径及执行时间。

前三份CSV及生成FITS应得到同一源身份、位置和缺省自行。人工自行表单独核对
mas/yr、arcsec/yr、cosδ和逐行历元，不能据此宣称真实观测自行修正已通过。
范围选择、观测集合、共享伙伴及Slurm计划属于第二阶段，本次不作验收。

请提供本次 `stage1_catalogue_validation.log` 的输出；若失败，一并保留
末尾打印的report.json，便于定位具体输入和检查项。
