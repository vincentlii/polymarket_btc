# BTC Stage Challenger 研究结果（2026-08-05）

本次研究使用已有的 BTC 15m 历史方向数据：7,295 个市场、262,620 个
快照、52 个特征，数据 hash 为
`8b6ad25d3fb81940f3b08b1d9445448acfd8f8a29d7e41efa9cc5c37010dd4db`。

## 协议边界

- 按 `3–30s`、`35–90s`、`95–180s` 三个阶段分别建模。
- 每个阶段比较两个 Logistic 基线和预注册的 18 个低容量 LightGBM 候选。
- 候选选择只使用开发期 OOF 预测；选中后才评估一次 sealed holdout。
- 当前数据跨度不足正式 `90/21/14/28` 协议，因此本次使用 quick
  `35/10/7/7/14` 切分。
- bootstrap 使用 500 次，仅作为快速开发判断，不满足正式 Go 门槛。

## 结果摘要

| 阶段 | 开发期选择 | sealed holdout 市场数 | Log loss | Brier | Accuracy |
| --- | --- | ---: | ---: | ---: | ---: |
| `3–30s` | Logistic `C=0.1` | 1,345 | 0.6862 | 0.2465 | 53.8% |
| `35–90s` | Logistic `C=0.1` | 1,345 | 0.6624 | 0.2346 | 61.1% |
| `95–180s` | Logistic `C=0.1` | 1,345 | 0.6360 | 0.2224 | 64.8% |

三个阶段均没有 LightGBM 候选同时满足替换 Logistic 的规则（Log loss、Brier、
校准和多重比较后的 paired CI）。因此本次没有晋级 LightGBM，也没有修改 VPS
当前模型。

## Artifact 与复现

可复现入口：`scripts/btc_stage_challenger_research.py`。

研究输出位于本地 `output/btc_short_horizon/research/` 下的
`stage-aware-taker-v2-quick-20260805-clean-095327a` 目录；每个阶段包含
`summary.json`、`predictions.parquet` 和经 SHA 校验的 `model/`。这些是研究
artifact，不是生产 champion，也没有上传到 VPS 的模型目录。

## 结论与下一步

本次完成了“立即分阶段开发研究”，说明三个时间阶段可以独立训练和比较，且当前
数据下简单 Logistic 仍比受控 LightGBM 稳健。它不能证明 Taker 净盈利，因为数据
没有完整历史 Polymarket 成交、延迟、盘口消耗和 adverse-selection 证据。

下一步是让 VPS 继续积累真实接收时间、盘口和 Paper 成交结果；本地再用更长历史
补足正式 `90/21/14/28` 协议，并在交易目标标签成熟后评估净 EV Challenger。
