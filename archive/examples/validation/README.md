# 分阶段验收表格样本

这些是四条人工构造的输入，名称、坐标、自行和历元只用于读取适配测试，
不是已确认有 ASKAP 观测的科学目标。后续真实查询/处理另选已知源。

| 文件 | 目的 |
| --- | --- |
| stage1_minimal.csv | 最小三列；无自行列、默认J2000 |
| stage1_mapped_degrees.csv | 不同列名、额外原始列，坐标仍为度 |
| stage1_sexagesimal.csv | 同一批零自行位置；RA为hourangle、Dec为deg |
| stage1_proper_motion.csv | 人工自行与逐行历元；MuAlphaStar为含cosδ的mas/yr |

前三份表的源身份、坐标、自行和历元等价。第四份只与前三份比较身份和
参考位置，其自行与历元有意不同。脚本另生成同一批零自行目标的FITS，
以及arcsec/yr和不含cosδ的自行测试输入。

001须作为字符串保留；Source A规范化为Source_A；另外两行保留名称中的
正负号。RA包含0度及接近360度，赤纬包含正负值。

在独立验收目录运行 `tests/validate_catalogue_stage1.py`，只调用实际新版
表格读取代码；每次创建新的run目录，保留原始列与标准化快照及report.json。
详细指令见 [第一阶段说明](../../docs/STAGE1_zh.md)。

## 第二阶段真实查询表

stage2_targets.csv使用正式版目录的真实J2000参考坐标：UCS689、UCS690、UCS693
和UCS225；改用通用测试名以验证无UCS列映射依赖。Reference_Name/Reference_ID
只作溯源。坐标来源为legacy/ozstar_askap/UltracoolSheet_Version2.1_index_unbinaryUCD.csv
的ra_j2000_formula/dec_j2000_formula；共享与边缘历史参考见Gate8验收文档。
此表无自行列，默认零自行，只用于真实query与冻结计划。后续科学处理应另行
配置实际自行/历元。执行指令见 [第二阶段说明](../../docs/STAGE2_zh.md)。

## 第三阶段真实自行与处理

stage3_sources.json保存上述前三源的目录PM值及J2000历元，来自同一正式版目录
的pmra_formula/pmdec_formula，PM参考Pena15。它用于实际单源/共享处理设置，
区别于Stage1人工数据和Stage2零自行输入。执行指令见
[第三阶段说明](../../docs/STAGE3_zh.md)。
