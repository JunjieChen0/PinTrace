# PinTrace

**Two Waves: SHA Pinning in GitHub Actions Workflows Following the tj-actions/changed-files Compromise**

研究 GitHub Actions 供应链事件之后 SHA 固定的研究代码。项目重建仓库默认分支的历史，测量每一条外部 `uses:` 引用及其变化，比较事故前后的事故基线，并分析响应对象、扩展范围与固定期的持续性。

作者：陈俊杰（Junjie Chen），南京理工大学计算机科学与工程学院。ORCID：[0009-0002-3499-3621](https://orcid.org/0009-0002-3499-3621)。

本仓库只包含测量与分析代码、测试和研究配置。论文（LaTeX 源、PDF、`claim_ledger.json` 与构建脚本 `paper/jsep_v2/tools/`）、全部结果报告、独立标注材料和数据都归档在 Zenodo，概念 DOI：[10.5281/zenodo.23523854](https://doi.org/10.5281/zenodo.23523854)（解析到最新版本）。在空克隆上不能重建论文结果，需要先放回 Zenodo 中的数据（见“复现数据”）。

## 版本对应

| 代码标签 | Zenodo 版本 | 版本 DOI | 说明 |
| --- | --- | --- | --- |
| `v1.1.1` | 1.8.2 | [10.5281/zenodo.24082196](https://doi.org/10.5281/zenodo.24082196) | 修订稿再投稿版（JSEP major revision） |
| `v1.1.1` | 1.8.1 | [10.5281/zenodo.24078055](https://doi.org/10.5281/zenodo.24078055) | 修订稿中间版本 |
| `v1.1.0` | 1.8.0 | [10.5281/zenodo.24076835](https://doi.org/10.5281/zenodo.24076835) | 新增覆盖审计、分层分析与独立标注 |
| `v1.0.8` | 1.7.0 | [10.5281/zenodo.23535531](https://doi.org/10.5281/zenodo.23535531) | 初投稿 |

## 内容

| 范围 | 模块 |
| --- | --- |
| 候选来源与队列资格 | `datasets`、`sources`、`frames`、`cohort`、`entity_identity` |
| 工作流与配置历史 | `measurement`、`lineage`、`configuration`、`verify_measurement` |
| 固定期、上游机会与维护 | `risksets`、`opportunities`、`opportunity_join`、`maintenance_scope`、`maintenance_activity` |
| 匹配对照 | `control_frame`、`control_baseline`、`control_match`、`control_measurement`、`control_compare` |
| 统计与推断 | `formal_stats`、`formal_extensions`、`formal_activity`、`formal_competing`、`formal_placebo`、`formal_sensitivity`、`formal_robustness`、`formal_acceptance` |
| 修订阶段的检查 | `post_review`（事前基线、集中度与第二波）、`denominator_audit`（分母口径）、`release_timing`（上游发布时间） |
| 依赖图覆盖审计 | `coverage_crawl`（爬取 `tj-actions/changed-files` 的公开依赖列表）、`coverage_audit`（抽样、资格判定、覆盖率与特征比较） |
| 分层敏感性 | `stratified_waves`（两次浪潮在活跃度、所有者类型与基线固定状态各层中的比率） |
| 效度与可复现 | `validity_golden`、`validity_reexport`、`validity_goldset` |
| 独立人工标注 | `tools/annotation/`：`make_packet.py`（离线标注包）、`codebook.md`、`guide.md`、`freeze.py`（冻结记录）、`score.py`（一致性与流水线比较） |
| 图表与历史工具重放 | `formal_figures`、`tool_replay` |

所有区间使用整仓库集群自助法（seed 与重复次数见 [study.toml](study.toml)）。竞争风险分析使用 Aalen–Johansen 累积发生率；敏感性矩阵覆盖 16 个指标与 5 个总体或锚点情景。每个报告同时记录输入哈希与规则版本。

## 安装与测试

需要 Python 3.11+ 和 Git。正式统计另需固定版本的数值依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]" -r requirements-formal.txt
python -m pytest -q
```

## 运行

采集需要 GitHub 认证：设置 `GH_TOKEN` 或安装 GitHub CLI 后运行 `gh auth login`。凭据只用于 API 请求，不写入源码。采集读取公开仓库的 Git 对象，不执行被研究仓库的工作流或脚本。

```powershell
python -m pintrace.datasets --download
python -m pintrace.pilot --limit 1 --workers 1
python -m pintrace.measurement --workers 1
```

下游按依赖顺序运行：

```powershell
python -m pintrace.verify_measurement --workers 1
python -m pintrace.risksets --require-complete
python -m pintrace.opportunities --repositories actions/checkout tj-actions/changed-files actions/setup-node actions/upload-artifact actions/setup-python actions/download-artifact actions/cache --archive-max-requests 90
python -m pintrace.opportunity_join
python -m pintrace.tables
python -m pintrace.maintenance_scope
python scripts/verify_analysis.py
python -m pintrace.formal_stats --working
python -m pintrace.formal_extensions --working
python -m pintrace.formal_activity
python -m pintrace.formal_competing
python -m pintrace.formal_placebo
python -m pintrace.formal_sensitivity
python -m pintrace.validity_golden
python -m pintrace.validity_reexport
```

`formal_stats --working` 读取工作输入清单并写入 `data/derived/formal-stats-v3/`；`formal_extensions --working` 写入 `formal-stats-v4/`。`risksets --require-complete` 在测量未齐时拒绝写出正式报告。大小仓库的完整快照会显著占用内存，测量与核验按单 worker 串行执行。

修订阶段新增的分析在上面的结果之后运行：

```powershell
python -m pintrace.post_review --working
python -m pintrace.denominator_audit
python -m pintrace.release_timing
python -m pintrace.stratified_waves
python -m pintrace.coverage_crawl                 # 依赖列表爬取，约 2.5 秒一页，可用 --resume 续爬
python -m pintrace.coverage_audit sample          # 固定 seed 抽取 300 个仓库
python -m pintrace.coverage_audit collect         # 克隆并按队列的基线规则判定资格
python -m pintrace.coverage_audit summarize
```

已发布的结果可以不重算而直接核对：

```powershell
python -m pintrace.coverage_audit check           # 输入与实现文件的哈希，以及重算的估计值
python -m pintrace.stratified_waves --check       # 重算各层并与已发布报告比较
python tools/annotation/freeze.py freeze-6 --check
```

独立标注的流程是：`make_packet.py` 生成离线标注包，`freeze.py` 在收到正式答案之前冻结标注包、codebook 与评分脚本，`score.py` 只接受最终两名标注者（A3、A4）的答案；若任何冻结文件改变，它在读取答案之前就会停止。具体命令见 Zenodo 归档中的 `README_DATA.md`。

## 复现数据

Zenodo 记录包含两个归档。测量归档（24 个分卷，合并说明见 `JOIN_MEASUREMENTS.md`）包含测量快照、上游证据与匹配对照。结果归档 `pintrace-results-v1.tar.zst` 包含正式结果文件、覆盖审计的爬取页面、样本与资格记录、分层分析、150 项金标集、400 项独立标注（标注包、codebook、冻结记录、A3 与 A4 的化名答案、共识与评分）、论文源与 `claim_ledger.json`。归档内的 `README_DATA.md` 列出文件、校验值与目录布局；把数据目录放回 `data/derived/` 后即可按上面的顺序重算或核对。

### 行尾与实现哈希

部分正式运行报告（`formal_acceptance.json` 等）记录的实现文件哈希，是在 Windows 工作区以 CRLF 行尾计算的；仓库与标签中的这些文件以 LF 保存，内容相同。在干净克隆上核对这些哈希（或运行会比对实现哈希的检查）之前，先把 [tools/crlf_hashed_files.txt](tools/crlf_hashed_files.txt) 列出的文件转换为 CRLF：

```bash
python -c "import pathlib; [pathlib.Path(p).write_bytes(pathlib.Path(p).read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')) for p in pathlib.Path('tools/crlf_hashed_files.txt').read_text().split()]"
```

其余文件（包括覆盖审计、分层分析和标注评分涉及的实现文件）的哈希按 LF 记录，无需转换。例外：`formal_competing.py`、`validity_golden.py` 和 `validity_reexport.py` 在 `formal_acceptance.json` 中按 CRLF 记录，在各自较早的报告（`formal_competing_risks.json`、`validity_golden.json`、`validity_reexport.json`）中按 LF 记录；核对这三份报告时使用检出的 LF 文件。

## 许可

源码使用 [MIT License](LICENSE)。Zenodo 数据归档使用 CC BY 4.0。
