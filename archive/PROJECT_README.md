# DStools_Auto_Processing

ASKAP visibility automation with two entry points sharing one scientific core:

- **`askap-batch`**: CSV/FITS catalogue, full-catalogue observation index,
  shared preprocess/models, independent target subtraction and DS/I/V products.
- **`askap-source`**: query one position, inspect candidates, then process saved
  selections. Preserves archives, original MS, processing copies, full models,
  full images, separate cutouts and logs.

The new implementation is in `askap_auto/`. It never imports the historical
UCD code in `legacy/`. The original sibling `Ozstar_ASKAP_UCDs` is unchanged.

## Start here / 使用入口

[完整中文操作说明](docs/USAGE_zh.md) · [Architecture and recovery](docs/ARCHITECTURE.md)
· [Validation and remaining site acceptance](docs/VALIDATION.md)
· [Ozstar deployment and pilot testing / 部署测试步骤](docs/DEPLOYMENT_zh.md)
· [Stage 1 offline catalogue acceptance / 第一阶段指令](docs/STAGE1_zh.md)
· [Stage 2 real query and plan acceptance / 第二阶段指令](docs/STAGE2_zh.md)
· [Stage 3 one-dataset processing / 第三阶段指令](docs/STAGE3_zh.md)
· [Stage 4A results and skip checks / 结果与MS时间检查](docs/STAGE4A_zh.md)
· [Stage 4B Ada transfer / Ada传输与回执检查](docs/STAGE4B_zh.md)
· [UCDs parameter equivalence and redundancy audit / 参数对照](docs/UCD_PARAMETER_AUDIT_zh.md)
· [User configuration](../config.py) · [Design: batch](GENERALIZATION_PLAN.md)
· [Design: single source](SINGLE_SOURCE_PLAN.md)

Requires Python >=3.10 with `requirements.txt`, an accepted DStools SIF,
Apptainer and Slurm on Ozstar. Install host dependencies on the **login node**;
compute nodes remain offline. Ada's `pull_products.py` uses only the standard
library and supports Python >=3.7.

```bash
# Login node: use the existing host environment.
cd /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
ASKAP_HOST_PYTHON=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/env/askap-python/bin/python
"$ASKAP_HOST_PYTHON" -m pip check
# If dependencies are missing, install requirements in this environment first.
# Edit config.py: WORK_ROOT, CATALOGUE_PATH, CONTAINER, PYTHON, CASDA_USERNAME.
"$ASKAP_HOST_PYTHON" askap-batch query
"$ASKAP_HOST_PYTHON" askap-batch run --submit
```

Single source does not read batch config or inherit `ASKAP_*` settings:

```bash
"$ASKAP_HOST_PYTHON" askap-source query --ra 180.1494 --dec=-28.6159 \
  --name MySource --work-dir /path/to/MySource
# Inspect /path/to/MySource/query/observations.csv.
"$ASKAP_HOST_PYTHON" askap-source run --work-dir /path/to/MySource \
  --settings /path/to/site.json --submit
# To expand selected SBs: add --all-beams-for-sb 35429 21677.
```

Without `--submit` or `--prepare-download`, `run` writes a frozen plan only.
The recommended remote code directory is `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing`.
The local checkout remains `Ozstar_DStools_Auto_Processing`. Other users should
override the host Python and site paths in their batch config or single-source JSON.
The login controller must remain alive to download/submit later batches;
resume the same workspace with `run --submit` after disconnecting.

No remote production validation is implied by the local test suite. The worker
checks configured command options against the actual SIF before scientific work.
Do not replace the accepted SIF merely to use this project. Optional historical
build recipes remain in `New_dstools_build/` and require explicit site adaptation.
