# Agent 接入与 Skill 工作流

本章确定后续接入规则，给出只读说明命令及 CLI 草案；不在当前仓库安装 Skill、Hook 或新脚本。

## Skill 名称与来源

命名格式为 `组织名-仓库名[-功能]`，功能段可省略。名称组件本身可以带连字符，不通过简单地数连字符推导组织和仓库；仓库身份应明确记录。

以目标组织 `patchouli-lib` 和仓库 `archive` 为例：

- `patchouli-lib-archive`：该知识库的基本接入和归档规则；
- `patchouli-lib-archive-search`：查找资料；
- `patchouli-lib-archive-add`：若以后有必要拆分，负责新增资料。

不强制立即拆出三个 Skill。使用覆盖实际需求的最少数量，避免重复描述同一规则。

首次接入安装仓库 `skills/` 提供的版本，不让不同 Agent 自行重写一份相似规则。安装到 Agent 的技能目录是正常使用方式，不把它降格为不参与加载的临时缓存。

## 共享脚本留在仓库根目录

通用执行脚本放在 `scripts/`。Skill 负责说明入口、参数、读取顺序和边界，不内嵌一份会独立过期的脚本副本。

使用时从可信仓库的已同步工作副本调用根目录脚本。若某个客户端选择下载脚本而非保留工作副本，必须按明确版本取得相关文件与依赖，不使用不固定版本的远端下载即执行命令。

技能安装目录与仓库工作目录可能不同，Skill 应明确要求先定位目标仓库，再运行脚本；不能把自身目录误当仓库根目录。

本地接入说明只记录仓库地址、工作副本位置、已安装 Skill 的来源版本以及凭据变量名或凭据管理器入口，不记录凭据值。

### 开始任务时

1. 核对当前仓库、分支和未提交修改，再同步远端信息；不自动丢弃、暂存隐藏或覆盖已有工作。
2. 阅读最新根 README、AGENTS 和目标分区 README；本地安装 Skill 不替代当前仓库规则。
3. 比较已安装 Skill 与仓库提供版本，有更新则同步；本地改过时先展示差异，再合并或请求用户处理。
4. 使用与本次仓库版本相符的根目录脚本。

版本比较可以使用 Skill 内容哈希或对应提交，不要求每次手工维护额外版本号。规则与脚本确有不兼容时停止该操作，说明需要同步的部分。

## 读取分区说明的多系统示例

以下命令只读取一级资料目录的 README，运行位置为知识库仓库根目录。示例约定排除隐藏目录，以及 `skills`、`scripts`、`node_modules`、`vendor`、`venv`、`__pycache__` 等保留工具目录；新增工具目录也应加入统一排除配置。

这些是人工排查和轻量阅读的快捷方式，不负责生成正式 INDEX，也不执行资料中的内容。输出保留路径分隔，方便判断每段说明属于哪个分区。

### Bash，适用于 Linux 与 macOS

```bash
for d in */; do case "$d" in skills/|scripts/|node_modules/|vendor/|venv/|__pycache__/) continue ;; esac; [ -L "${d%/}" ] && continue; p="${d}README.md"; if [ -f "$p" ]; then printf '\n--- %s ---\n' "$p"; cat "$p"; fi; done
```

默认不启用隐藏目录 glob。若用户 shell 改过该选项，需同时排除点开头目录；正式 CLI 应独立实现此规则，不依赖 shell 全局选项。

### PowerShell，适用于 Windows 或已安装 PowerShell 的其他系统

```powershell
Get-ChildItem -Directory | Where-Object { $_.Name -notmatch '^\.' -and $_.Name -notin @('skills','scripts','node_modules','vendor','venv','__pycache__') -and -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } | Sort-Object Name | ForEach-Object { $p = Join-Path $_.FullName 'README.md'; if (Test-Path -LiteralPath $p -PathType Leaf) { Write-Output ('--- ' + $_.Name + '/README.md ---'); Get-Content -LiteralPath $p -Encoding UTF8 } }
```

### Windows CMD

CMD 版本调用 Windows PowerShell 执行等价只读操作，避免自行拼接 `for /f` 导致含空格路径被截断。以下在 CMD 中直接粘贴，不在外层再包一层 PowerShell 字符串：

```bat
powershell.exe -NoProfile -Command "Get-ChildItem -Directory | Where-Object { $_.Name -notmatch '^\.' -and $_.Name -notin @('skills','scripts','node_modules','vendor','venv','__pycache__') -and -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } | Sort-Object Name | ForEach-Object { $p = Join-Path $_.FullName 'README.md'; if (Test-Path -LiteralPath $p -PathType Leaf) { Write-Output ('--- ' + $_.Name + '/README.md ---'); Get-Content -LiteralPath $p -Encoding UTF8 } }"
```

### Python 单行版本

以下需要 Python 3.12 或更新版本，以便同时识别 Windows junction；按实际安装将 `python` 替换为 `python3`，或可用时的 `py -3`。这只是说明遍历命令，不决定未来检索客户端的最低 Python 版本。

```text
python -X utf8 -c "from pathlib import Path; skip={'skills','scripts','node_modules','vendor','venv','__pycache__'}; ds=sorted((d for d in Path('.').iterdir() if not d.name.startswith('.') and d.name not in skip and not d.is_symlink() and not d.is_junction() and d.is_dir()),key=lambda d:d.name.casefold()); [print('\n--- '+p.as_posix()+' ---\n'+p.read_text(encoding='utf-8-sig')) for d in ds for p in [d/'README.md'] if p.is_file()]"
```

快速阅读命令适用于可信工作副本；正式生成器还应识别 README 文件本身的链接和仓库边界，不能借符号链接读取仓库外材料。

## 新增资料

1. 读根规则和分区说明，选择归属；确实没有合适分区时再创建用途明确的新分区。
2. 写主文档、元数据和可选附件目录，使用相对链接。
3. 运行提交前工具；由它生成 INDEX、检查文件名、元数据和本地链接。
4. 按诊断修正文档，查看修改，只暂存本次授权的资料及对应生成结果。
5. 正常提交和推送。工作 Agent 不调用向量入库、删索引或人工更新搜索数据库。

## 查找资料

先读仓库规则；不清楚分区时读取一级分区 README。随后使用检索客户端查关键词、语义或混合结果，查看相关分区说明与索引，再读原文和必要附件。

结果应提供仓库、原文路径、标题、命中位置和索引对应提交，不能只给没有来源的模型总结。检索不可用时可在已有本地工作副本中使用 `git grep` 或 `rg`，并说明该副本的新旧状态；不把“搜索服务失败”解释为“资料不存在”。

### CLI 设计示例

以下入口尚未实现，用于说明职责，不应直接当作可执行命令：

```text
python scripts/library.py overview
python scripts/library.py index --write
python scripts/library.py check
python scripts/search.py --mode keyword --keywords release validation
python scripts/search.py --mode hybrid --keywords release --query "查找升级后的验收结果"
```

Skill 中的正式说明必须分别提供 Bash、PowerShell、CMD 的正确引号写法，并说明 Python 启动器差异。不要假定所有系统支持 Bash、POSIX 管道或相同环境变量语法。

## 多 Agent 与合并

每个 Agent 使用独立工作副本。新增独立文档通常比共同修改大表更少冲突；INDEX 不由模型手工拼接。

合并时先处理正式文档，再从合并后的资料重新生成 INDEX。正文冲突必须阅读双方内容，不能一律接受“我的版本”或“远端版本”；事实或意图冲突无法确定时交给用户判断。

目录重组、重命名和批量修订链接作为独立操作，不混进普通归档。禁止强推覆盖别人、删除一方内容凑成无冲突结果，或为同步 Skill 而覆盖用户本地修改。
