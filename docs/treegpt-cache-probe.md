# TreeGPT 缓存探针（独立项目）

CC resident 上下文去重（PR #90）验收完成后启动。**不**把 TreeGPT / `api_relay` 策略混进 CC resident。

## 目标

侧路测量 TreeGPT（`ACTIVE_RELAY=tree` → `https://api.treegpt.cc/v1/messages`）在生产拼装路径下的短聊缓存表现：

| 指标 | 含义 |
|------|------|
| 冷启动 `cache_creation` | 首轮写入前缀 |
| 热轮 `cache_creation` 中位 | 是否每轮重写前缀 |
| 热轮 `cache_read` | 是否命中已写缓存 |

## 约束

- **不改** `GW_PROVIDER`（生产可继续 `claude_code`）
- **不写** `chat_messages`
- 复用：`build_system(split_dynamic=True)` + rolling BP4 + `metadata.user_id=hayana-fyodor-stable`
- 能力表：`relay/capabilities.py` → treegpt `cache=True`，`cache_1h=False`

## 怎么跑

在 VPS（`/opt/frontend` 或已同步本仓库）：

```bash
cd /opt/frontend   # 或仓库根
python3 scripts/treegpt_cache_probe.py --turns 6
python3 scripts/treegpt_cache_probe.py --turns 6 --prod-like   # 带 tools + 小 thinking
```

结果 JSON 默认写到 `artifacts/treegpt-cache-probe-*.json`。

## 对照基准

| 来源 | 热轮 `cache_creation` |
|------|----------------------|
| CC resident 修前 | ≈9771 |
| CC resident 验收后中位 | ≈214.5 |
| TreeGPT 历史健康段（2026-07-17 23:06–23:15，msg 3777–3785） | **0**（`cache_read`≈32965） |
| TreeGPT 历史污染段（更早，create 常 40k–180k） | 每轮重写 |

探针通过标准（初版）：热轮中位 **≤ 1k**，且明显低于修前 CC 的 ~10k；理想为接近历史健康段的 **0**。

## 启动日结果（2026-07-18）

详见 `artifacts/treegpt-cache-probe-baseline.json`（余额阻断记录）与 `artifacts/treegpt-cache-probe-live.json`（充值后重跑）。

### 充值后 live（opus / split_dynamic / 无 tools）

| 轮次 | phase | cache_creation | cache_read |
|------|-------|----------------|------------|
| T1 | cold | 0 | 16242 |
| T2 | hot | 0 | 16268 |
| T3 | hot | 89 | 16268 |
| T4 | hot | 208 | 16242 |
| T5 | hot | 124 | 16357 |
| T6 | hot | 31 | 16481 |

- 热轮 `cache_creation` 中位 **89**（mean 90.4，max 208）→ **通过**（阈值 ≤1k）
- `GW_PROVIDER` 全程 `claude_code`
- 对照：CC 修前 ≈9771 / 修后 ≈214.5 / TreeGPT 历史健康段 0

### 此前余额阻断

1. **opus 首次 live**：余额约 $0.05 → 403。
2. **历史健康段（msg 3777–3785）**：热轮 create 中位 0。

重跑命令：

```bash
cd /opt/frontend
python3.11 scripts/treegpt_cache_probe.py --turns 6 --out artifacts/treegpt-cache-probe-live.json
```

## 已知生产路径

`gateway.py` api_relay 分支已：

1. `split_dynamic=True`：system 只留 BP1 + 稳定说明；BP2/BP3 → 当前 user 前缀
2. `_apply_rolling_cache_control`：在倒数第二条 user 上打 BP4
3. `metadata.user_id` 稳定

本探针验证的是：**当前代码 + TreeGPT 中转**是否仍保持健康命中，以及 `--prod-like`（tools 波动）是否拉高 creation。

## 后续（探针出数后再排）

- 若默认模式健康、prod-like 劣化 → 查工具抽屉/工具列表稳定性
- 若两者都差 → 查 system 块字节稳定性、adapter 是否剥掉 `cache_control`、user_id
- 优化方案另开 PR；仍禁止混入 `cc_resident`
