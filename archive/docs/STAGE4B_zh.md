# Stage4B：Ada真实拉取与回执闭环

2026-10-15：用户接受Stage4A已记录的数值差异，结束进一步数值复核。
原自动报告保持REVIEW，接受决定另存tests/stage4a-acceptance.json。
不改科学参数、比较阈值或原Stage3产物，直接进入本阶段。

## 验收范围

复用Stage3B的Anchor_A、Partner+02、Partner-03，同一SB74372/beam28：

1. 确认远端为已通过的三源Stage3B案例，具有3套完整产物。
2. 首次调用现有pull_products.pull，预期传输9件DS/I/V并校验SHA/大小、上传回执。
3. 再次调用同一程序，预期3套完成、0件科学文件需传输。
4. 核对Ada台账、9件文件指纹、两次本地回执及当前版本链接。
5. 经SSH调用Ozstar现有State.export导入两份新回执，确认三个指定attempt均为
   transfer_status=verified，两个新receipt ID都入库，attempt/run/job数量未增加。
   最后重查远端科学文件指纹未改变。

脚本tests/validate_transfer_stage4b.py是独立站点检查器，直接复用生产拉取程序；
不复制一套传输实现。本轮不做文件损坏注入，不下载MS、不运行SIF、不提交Slurm。
Ozstar侧预期写入两次传输回执、传输状态及进度导出，不改科学产物。

## 已完成的现场验收

2026-10-15用户提供Ada Astro环境的完整控制台输出：五项检查全部PASS，最终
STAGE4B PASS。首次传输9件文件，重复传输0件；Ada台账、文件SHA/大小、回执与
当前版本链接通过，Ozstar成功导入两份新回执并确认三个目标传输已验证。
实际报告路径：

`/PATH/TO/ada/DStools_Auto_Processing_validation/stage4b/run-v16e3ccc/report.json`

当前证据为用户提供的现场输出，完整report.json尚未复制到本地。正常传输闭环已完成，
本轮正常传输无需重复；随后经代码/回归核对，将独立Stage4C现场验证改为可选，
本轮跳过、不阻塞正式使用。具体依据及未覆盖范围见[Stage4安排](STAGE4_PLAN_zh.md)。

## 1. Mac上传新脚本

~~~bash
cd /PATH/TO/DStools_Auto_Processing
./deploy.sh YOUR_OZSTAR_USER@ozstar.swin.edu.au /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing
~~~

## 2. Ada准备并执行

以下整段在Ada运行，可在tmux中执行；独立Bash内的失败会停止本次操作，不退出外层shell，
并显示失败行号和退出码。Bash继承当前已激活环境的PATH。
复用既有Ozstar SSH密码文件，默认沿用旧Gate10的路径；位置不同时只修改PASSWORD_FILE。
这是Ozstar SSH密码文件，不是CASDA密码文件。也支持已有SSH key/agent（不传password-file）。

不需要复制config.py或安装科学Python环境，Ada只需Python>=3.7、ssh/scp/rsync和
密码方式所需的sshpass。Ozstar回执导入使用既有宿主Python和相同模块。

~~~bash
bash <<'BASH'
  set -Eeuo pipefail
  trap 'stage4b_rc=$?; printf "[4B] ERROR: line=%s exit=%s\n" "$LINENO" "$stage4b_rc" >&2' ERR
  ADA_BASE=/PATH/TO/ada
  PASSWORD_FILE="$ADA_BASE/state/ozstar-transfer-password"
  STAGE4B_BIN="$ADA_BASE/DStools_Auto_Processing_validation/bin"
  STAGE4B_ROOT="$ADA_BASE/DStools_Auto_Processing_validation/stage4b"

  printf '[4B] 检查依赖\n'
  missing=0
  for tool in python3 ssh scp rsync sshpass; do
    if tool_path=$(command -v "$tool"); then
      printf 'OK: %s -> %s\n' "$tool" "$tool_path"
    else
      printf 'MISSING: %s\n' "$tool" >&2
      missing=1
    fi
  done
  if [ "$missing" -ne 0 ]; then
    printf '依赖不完整；Stage4B尚未启动。\n' >&2
    exit 1
  fi
  python3 -c 'import sys; print(sys.version); sys.exit("Python >=3.7 required" if sys.version_info < (3, 7) else 0)'
  if [ ! -s "$PASSWORD_FILE" ]; then
    printf '未找到已有Ozstar SSH密码文件，请修改PASSWORD_FILE：%s\n' "$PASSWORD_FILE" >&2
    exit 1
  fi
  chmod 600 "$PASSWORD_FILE"
  mkdir -p "$STAGE4B_BIN" "$STAGE4B_ROOT"

  for remote_file in pull_products.py tests/validate_transfer_stage4b.py; do
    printf '[4B] 复制 %s\n' "$remote_file"
    sshpass -f "$PASSWORD_FILE" scp -4 -o ConnectTimeout=30 \
      "YOUR_OZSTAR_USER@ozstar.swin.edu.au:/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing/$remote_file" \
      "$STAGE4B_BIN/"
    printf '[4B] 复制成功：%s\n' "$remote_file"
  done

  printf '[4B] 检查脚本语法并启动验收\n'
  python3 -m py_compile "$STAGE4B_BIN/pull_products.py" "$STAGE4B_BIN/validate_transfer_stage4b.py"
  python3 -u "$STAGE4B_BIN/validate_transfer_stage4b.py" \
    --host ozstar.swin.edu.au \
    --user YOUR_OZSTAR_USER \
    --remote-work /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage3/shared3_SB74372_beam28 \
    --remote-code /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing \
    --host-python /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/env/askap-python/bin/python \
    --password-file "$PASSWORD_FILE" \
    --output-root "$STAGE4B_ROOT" \
    2>&1 | tee "$STAGE4B_ROOT/stage4b.log"
BASH
~~~

脚本创建全新run-*工作区，自动完成首次/重复拉取及Ozstar状态导入，不需要分别
切换到Ozstar执行progress。远端库存使用/usr/bin/python3标准库；导入核心状态时
单独加载gcc/13.3.0、python/3.12.3并使用上面的宿主解释器。

## 3. 结果

预期5个PASS，最后STAGE4B PASS。状态文件为本次run-*/report.json，报告记录两个
脚本的SHA、首次/重复文件数、回执位置和Ozstar登记结果；详细拉取日志在同目录。

~~~bash
cat /PATH/TO/ada/DStools_Auto_Processing_validation/stage4b/stage4b.log
~~~

失败时保留新工作区和report.json，查看first-pull.log、repeat-pull.log或
transport-error.log，不删除远端科学文件或手动修改数据库。
若在Stage4B启动信息之前结束，先查看步骤提示和ERROR退出码；工具存在、密码文件非空
仅代表前置文件检查通过，不代表SSH认证成功。两个脚本分别调用scp，分别显示复制结果。
重复整个命令会创建另一个全新验收run；同一次验收内部的第二次pull才是零重复测试。
