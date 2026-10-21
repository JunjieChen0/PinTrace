# PinTrace

**After the Pulse: SHA Pinning in GitHub Actions Workflows Following the tj-actions/changed-files Compromise**

研究 GitHub Actions 供应链事件之后 SHA 固定的研究代码。项目重建仓库默认分支的历史，测量每一条外部 `uses:` 引用及其变化，比较事故前后的事故基线，并分析响应对象、扩展范围与固定期的持续性。

作者：陈俊杰（Junjie Chen），南京理工大学计算机科学与工程学院。ORCID：[0009-0002-3499-3621](https://orcid.org/0009-0002-3499-3621)。

论文正在准备 IEEE Transactions on Software Engineering 投稿。复现数据与结果归档于 Zenodo，DOI：[10.5281/zenodo.23523854](https://doi.org/10.5281/zenodo.23523854)（当前版本 1.1）；本仓库只包含源码、测试与研究配置，不包含原始测量、报告、论文或数据缓存。

## 内容

| 范围 | 模块 |
| --- | --- |
| 候选来源与队列资格 | `datasets`、`sources`、`frames`、`cohort`、`entity_identity` |
| 工作流与配置历史 | `measurement`、`lineage`、`configuration`、`verify_measurement` |
| 固定期、上游机会与维护 | `risksets`、`opportunities`、`opportunity_join`、`maintenance_scope`、`maintenance_activity` |
| 匹配对照 | `control_frame`、`control_baseline`、`control_match`、`control_measurement`、`control_compare` |
| 统计与推断 | `formal_stats`、`formal_extensions`、`formal_activity`、`formal_competing`、`formal_placebo`、`formal_sensitivity`、`formal_robustness`、`formal_acceptance` |
| 效度与可复现 | `validity_golden`、`validity_reexport`、`validity_goldset` |
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

## 复现数据

Zenodo 归档包含正式结果文件、测量快照、上游证据、匹配对照、150 项金标集、论文源与 `claim_ledger.json`。归档内的 `README_DATA.md` 列出文件、校验值与解压后的目录布局；把测量目录放回 `data/derived/` 后即可按上面的顺序重算。源码仓库本身不能在空克隆上重建论文结果。

## 许可

源码使用 [MIT License](LICENSE)。Zenodo 数据归档使用 CC BY 4.0。
