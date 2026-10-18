# PinTrace

用于研究 GitHub Actions 供应链事件后的 SHA 固定、依赖扩展与持续维护的历史分析工具。

本仓库包含采集、测量、统计、图表生成和证据审计源码，以及配套测试和研究配置。计划书、内部过程文档、分析报告、论文与数据缓存不随源码发布。

## 安装与测试

需要 Python 3.11+ 和 Git；当前在 Windows PowerShell 环境验证。完整测试需要同时安装正式统计依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]" -r requirements-formal.txt
python -m pytest -q
```

`requirements-formal.txt` 固定 NumPy、SciPy 和 Matplotlib 版本。需要处理 JSS 的 Arrow 数据时，另安装 `python -m pip install -e ".[audit]"`。

## 最小采集入口

采集需要 GitHub 认证：设置 `GH_TOKEN`／`GITHUB_TOKEN`，或安装 GitHub CLI 后运行 `gh auth login`。不要将凭据写进源码。

```powershell
python -m pintrace.datasets --download
python -m pintrace.pilot --limit 1 --workers 1
```

首条命令下载并校验候选来源，下载量约 1.3 GB；第二条只采集一个候选，用于检查运行环境。采样规则、目标 Action 和观察窗口位于 [study.toml](study.toml)。认证信息只用于 GitHub API 请求；普通历史采集读取公开仓库的 Git 对象，不执行被研究仓库的工作流或脚本。

## 主要模块

| 范围 | 模块 |
| --- | --- |
| 候选来源与历史资格 | `datasets`、`sources`、`frames`、`cohort`、`entity_identity` |
| 工作流与配置历史 | `measurement`、`lineage`、`configuration`、`verify_measurement` |
| 固定期与上游机会 | `risksets`、`opportunities`、`opportunity_join`、`maintenance_scope` |
| 统计与论文产物 | `formal_stats`、`formal_figures`、`formal_manuscript` |
| 验收与证据封存 | `formal_acceptance`、`formal_submission`、`scripts/verify_analysis.py` |

`formal_stats` 和证据封存流程需要另行准备匹配的冻结清单、原始历史、上游证据及相关研究输入。它们会核对输入哈希；源码仓库本身不包含完整研究复现数据包，不能在空克隆上直接重建论文结果。

历史工具重放由 `tool_replay` 提供，属于可选入口。它需要 Windows x64、Git 和 7-Zip，会准备并运行指定历史版本的第三方 Node/Ruby 工具；受控上游接口不等于系统级沙箱。

## 本地产物

`data/`、`paper/`、`reports/` 和 `docs/` 均已忽略。采集结果、论文文件、报告、计划和研究过程记录保留在本地，不进入提交。
