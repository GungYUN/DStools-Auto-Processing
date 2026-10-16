# UCDs 参数对照与冗余检查

日期：2026-10-14。基线为原项目 Ozstar_ASKAP_UCDs 的实际科学命令、配置和
已接受 SIF 的构建源码，不修改原项目或重建 SIF。
可复核的命令、CLI 类型/默认值及八份来源文件 SHA 保存在
`tests/fixtures/ucd_science_contract.json`。

## 1. 本次失败原因

现场 commands.log 明确报告：

```text
Error: Invalid value for '-t' / '--threshold': '3.0' is not a valid integer.
```

UCDs 命令不传 threshold/mask-threshold，使用 DStools 的整数默认值3/5。
新版此前将默认值重复显式传为3.0/5.0；Click 在开始建模之前拒绝这些字符串。
这属于新版命令封装的回归。日志中的重复短选项-d警告来自DStools已有声明，
不构成本次退出原因；新版继续用无歧义的长选项，不修改SIF。

此前离线科学工具使用模拟器，help检查主要检查选项存在性，未覆盖CLI整数解析。
本次增加实际Click解析比较：旧短选项命令和新长选项命令解析后的全部值必须相等。

## 2. 默认输入逐项对照

| 项目 | UCDs生效值 | 新版处理 |
| --- | --- | --- |
| preprocess | ASKAP默认FIELD/FEED/DATA修正 | 同一工具、无附加科学参数 |
| band | AK_low | 保留显式值 |
| iterations | 100000 | 保留显式整数 |
| threads | 8/槽 | 保留显式整数 |
| multiscale | 开启 | 保留显式开关 |
| 建模minuvw-m | 200.0米 | 保留相同输入 |
| imsize/cell | 按AK_low/6km自动计算 | 不添加默认覆盖 |
| mgain/robust | 0.8/0.5 | 使用相同SIF默认值 |
| threshold/mask-threshold | 整数3/5 | 默认不重复传参；自定义整数正确输出 |
| 成像数据列 | corrected | 使用相同SIF默认值 |
| channels-out/deconvolution-channels | 8/8 | 使用相同SIF默认值 |
| subimages/spectral-pol-terms | 1/3 | 使用相同SIF默认值 |
| multiscale-scale-bias/max-scales | 0.7/8 | 使用相同SIF默认值 |
| 外部mask/erode/local-rms-window | 无/关闭/无 | 不添加默认覆盖 |
| 建模phasecentre | 原MS中心 | 不额外指定中心 |
| parallel-gridding/reordering | 工具默认 | 不额外添加 |
| verbose/savelogs | 开启/关闭 | 恢复verbose，删除额外savelogs |
| 模型目录/前缀 | wsclean_model/wsclean | 使用工具默认，删除重复out-dir/name |
| insert mask-pos | 各目标修正坐标 | 位置仍由源与观测时刻计算 |
| mask-radius | 工具按波束计算 | 默认不传额外半径 |
| subtract | split-ms开启 | 保留固定独立残差MS |
| DS数据列 | residual DATA | 使用相同工具默认DATA |
| DS minuvdist | 500米 | 输入500，与UCDs相同 |
| DS flags/baseline-average | 保留flags/平均基线 | 默认不传多余开关 |
| primary-beam | 不校正 | 不自动加入PB文件或校正 |
| I/V裁图 | 10×10 arcmin | 数值与Cutout2D/WCS行为相同 |

新版默认建模命令只保留与UCDs等效的参数：

```text
dstools-create-model --verbose --band AK_low --iterations 100000
  --multiscale --minuvw-m 200.0 --threads 8 --abs-mem <每槽预算>
  --temp-dir <临时目录> <MS>
```

没有修改上述默认科学数值。仍允许按既定通用化方案显式修改受支持选项；
只有与默认值不同的可选项才追加到命令。所有INTEGER选项统一检查：不接受布尔值、
小数或非法数，不四舍五入；旧冻结配置中的3.0/5.0等整数值可正确处理，不改写快照。

## 3. 资源与必要差异

正式默认仍为最多4槽、8线程/槽、160GiB总内存、10GiB临时盘、48小时上限。
估时仍为2小时/观测、0.5小时/目标、2小时固定开销、20分钟退出余量。
OpenBLAS固定1线程，OMP/MKL/NUMEXPR使用每槽8线程。

发现旧版按实际作业槽数分配内存，新版此前按配置的最大槽数分配；已纠正。
现在按申请的总MiB除以实际槽数，WSClean取该预算的75%，与UCDs一致；
实际槽数写在作业manifest，公共模型history记录建模时的资源预算。
当前Stage3仍是1槽、40GiB、WSClean30GiB，没有改变该案例资源。

通用表格、源名称、显式历元/单位/cosδ、Astropy自行传播、移除UCD专用筛选、
单源原始输入/未掩膜模型/完整图像保留和独立cutout，属于已确认两份方案的需求。
本次没有回退这些功能，也不宣称不同历元算法生成的坐标或新旧科学文件逐字节一致。
原始MS内的归档硬链接允许保留；原始/公共/私有科学副本之间仍使用真实复制。

## 4. 已删除的冗余

- 默认命令重复输出mgain、threshold、robust、数据列、通道数、谱项、
  多尺度默认子参数等，已改为依赖相同SIF默认值。
- 删除额外savelogs、重复out-dir/name；命令日志已有统一保存。
- DS不再重复指定默认DATA或baseline-average=True；仅用户修改时传相应选项。
- help只核对本次会用到的选项，不再强制检查未使用的DS开关。
- 批量目标的模型已是私有副本，直接在它上面insert；删除额外insertion_model复制。
  单源仍保留独立插模副本，以满足完整未掩膜模型保留要求。
- 自动bind删除已被父目录覆盖的子目录；用户显式bind保持有效。
- config.py的MODEL示例只列UCDs显式参数，避免重复维护整套工具默认值。
- 两次已确认的接口失败共用一个retry准备流程，不复制一套长期分叉的恢复程序。

查询缓存、冻结配置、SQLite、作业manifest、完成版本/回执和失败历史承担不同职责；
没有删除这些追溯与恢复机制。Ada配置实际由pull_products.py读取，已核实不是死配置。

## 5. 验证与继续执行

121项本地测试通过，含实际Click解析的四个工具基线等价性、可配置覆盖项、
全套INTEGER类型、少于配置上限时的实际内存分配、单源保留和三目标共享。
这些检查不代替Ozstar的真实MS/SIF运行。用户随后提供单源案例STAGE3 PASS，
确认修正后的真实处理与保留/指纹协议通过；下一步为三源共享案例。

本次恢复使用现有Stage3目录的retry识别已确认的阈值解析失败，保存旧日志和
状态，冻结retry_number=2的独立尝试。它继续使用原SIF/配置/选定数据集及下载SHA，
复用已有原始MS；不重新下载、不自动提交、不重置重试次数。
随后用run --submit执行；具体指令见[Stage3说明](STAGE3_zh.md#62-阈值参数失败后的恢复)。
