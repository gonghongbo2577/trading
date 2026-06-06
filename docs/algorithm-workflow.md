# 多因子选股策略 — 完整算法逻辑流图 v2.0

> 基于 `docs/strategy-plan-v2.md` v2.0 和 `docs/tech-plan.md` v2.0 交叉验证生成
> 所有算法节点均标注源文档出处，禁止虚构
> 最后同步: 2026-06-04 (strategy-plan-v2.md v2.0 系统性优化修订)

---

## 一、系统总体架构（数据流）

> 来源: tech-plan.md v2.0 §2.1 整体数据流, §2.2 模块依赖关系

```mermaid
flowchart TD
    A["Tushare Pro API\n2120 积分"] --> B["data_loader.py\n下载 + 清洗 + 缓存"]
    A2["BaoStock API\n免费备选数据源"] --> B2["data_loader.py\n交叉验证下载"]
    B --> C["Parquet 分区存储\ndata/raw/"]
    B2 --> C2["Parquet 分区存储\ndata/raw/baostock_daily/"]
    C --> D["DuckDB / Polars\n数据加载"]
    C2 --> VALIDATION["双源交叉验证\nTushare vs BaoStock\n日收益相关性 ≥ 0.99"]
    D --> E["factors.py\n[v2.0] BP+/残差MOM/ROE/IV60/ln Size\n+ 行业感知去极值\n+ 全因子VMP缩放\n+ IC衰减引导权重"]
    D --> F["universe.py\n[v2.0] 十层过滤构建可投资池"]
    E --> G["backtest.py\n[v2.0] 事件驱动回测引擎\n+ Walk-Forward + PBO + DSR"]
    F --> G
    G --> H["risk.py\n[v2.0] 风控模块\nMA20双条件 + 恢复迟滞 + 15%熔断"]
    G --> I["performance.py\n绩效分析"]
    G --> J["report.py\n报告生成"]
    H --> G
```

**关键依赖顺序**（tech-plan.md v2.0 §2.2）：
```
config.py → data_loader.py → {universe.py, factors.py} → backtest.py → {risk.py, performance.py, report.py}
```

---

## 二、数据下载全流程（Phase 1 核心）

> 来源: tech-plan.md v2.0 §3.1 数据下载流程, §3.2 各步骤详细设计

```mermaid
flowchart TD
    START["开始数据下载"] --> S1["步骤1: 验证 Tushare Token + BaoStock 连接\npro_api(token).stock_basic() + bs.login()"]
    S1 --> S2["步骤2: 下载全量股票列表\n合并 L + D + P 三种状态\n← stock_basic(list_status='L'/'D'/'P')"]
    S2 --> S3["步骤3: 按交易日批量下载日线\npro.daily(trade_date='YYYYMMDD')\n每次 ~5000行, 间隔0.35s\n分区: date=YYYYMMDD/data.parquet"]
    S3 --> S4["步骤4: 下载 daily_basic 估值指标\nPE/PB/PS/总市值/流通市值/换手率\n分区: date=YYYYMMDD/data.parquet"]
    S4 --> S5["步骤5: 下载 fina_indicator 财务指标\nROE/ROA/毛利率/净利率/EPS\n+ [v2.0] goodwill/audit_opinion/total_equity\n保留 ann_date 用于 PIT 过滤\n分区: end_date=YYYYQQ/data.parquet"]
    S5 --> S6["步骤6: 下载申万行业分类\nindex_classify(level='L1', src='SW2021')\n+ index_member_all()"]
    S6 --> S7["步骤7: 下载中证500指数行情\n用于基准和择时信号"]
    S7 --> S8["[v2.0] 步骤8: 下载 pledge_stat 股权质押数据\npledge_stat API → 质押比例\n供 Universe 过滤用"]
    S8 --> S9["[v2.0] 步骤9: 下载 BaoStock 日线\n相同区间，用于交叉验证\n分区: baostock_daily/date=YYYYMMDD/"]
    S9 --> S10["步骤10: 数据质量校验\nvalidate_data() + [v2.0] 双源交叉验证"]
    S10 --> VALID{"全部校验通过?\n含 Tushare vs BaoStock\n相关性 ≥ 0.99"}
    VALID -->|是| DONE["数据就绪"]
    VALID -->|否| FIX["修复数据问题"] --> S10
```

**步骤3 关键验证**（tech-plan.md v2.0 §1.2）：
- `pro_bar` 一次只能查询一只股票，不可批量
- 推荐使用 `pro.daily(trade_date='YYYYMMDD')` 按交易日批量下载
- 10年约2450个交易日 → ~2450次调用 → ~14分钟
- 速率控制: `time.sleep(0.35)` (200次/分钟, 留余量)

**[v2.0] BaoStock 交叉验证**（tech-plan.md v2.0 §1.3）：
- `pip install baostock`，零门槛，无需 token
- 使用涨跌幅复权法（与 Tushare 现金红利复权存在系统性差异）
- 仅用于交叉验证（比较日收益相关系数），不用于主策略计算
- Tushare 断服时可作为应急替代

**数据质量校验清单**（tech-plan.md v2.0 §3.4）：
- 股票列表按 ts_code 去重，无重复
- 每只股票有明确的 list_date 和 delist_date
- 日线每个 (ts_code, trade_date) 唯一
- 无未来日期数据
- 无负数价格或成交量为0的异常记录
- 退市股退市日期后无新数据
- 复权因子连续递增
- fina_indicator 的 ann_date ≥ end_date
- [v2.0] fina_indicator 含 goodwill, total_equity, audit_opinion 字段
- [v2.0] pledge_stat 数据覆盖全市场（> 4000 只股票）
- [v2.0] Tushare vs BaoStock 日收益 Pearson 相关系数 ≥ 0.99

---

## 三、可投资池构建（[v2.0] 十层过滤）

> 来源: strategy-plan-v2.md §4.1 第一步：构建可投资池

```mermaid
flowchart TD
    START["全市场 A 股 (~5000只)"] --> F1["① 排除 ST / *ST\n原因: 5%涨跌幅、流动性差、退市风险\n→ 剩 ~4800"]
    F1 --> F2["② 排除上市不满1年次新股\n原因: 无足够历史数据(残差动量需6月,低波需60日)\n→ 剩 ~4400"]
    F2 --> F3["③ [v2.0] 排除非CSI 300/500/1000成分股的科创板\n原因: 放宽原完全排除。若已是主要指数成分(如中芯国际/金山办公)则纳入\n约束: 科创板持仓上限 = 2/10 (20%), 单只科创板按200股/手\n→ 剩 ~3950 (比v1.3多~50只优质科技股)"]
    F3 --> F4["④ 排除 PE_TTM ≤ 0 的亏损股\n原因: EP因子对亏损股无意义\n→ 剩 ~2800-3200"]
    F4 --> F5["⑤ 排除近20日日均成交额 < 2000万\n原因: 流动性不足,冲击成本可控\n→ 剩 ~1800-2200"]
    F5 --> F6["⑥ 排除股价 < 2元\n原因: 面值退市红线1元,安全垫过薄\n→ 剩 ~1750-2150"]
    F6 --> F7["⑦ 排除净资产 ≤ 0\n原因: 资不抵债,财务类退市风险\n→ 剩 ~1700-2100"]
    F7 --> F8["⑧ [v2.0 新增] 排除最近年度被出具非标审计意见\n原因: 2024年A股3.5%公司收到非标意见(CSRC数据)\n非标意见是财务造假/退市的最强预警信号\n→ 剩 ~1650-2050"]
    F8 --> F9["⑨ [v2.0 新增] 排除商誉/净资产 > 30%\n原因: 商誉减值风险——2024年全市场商誉减值638亿元\n高风险行业: 医药/非银金融/电子\n→ 剩 ~1550-1950"]
    F9 --> F10["⑩ [v2.0 新增] 排除股权质押比例 ≥ 50%\n或控股股东质押 ≥ 80%\n原因: 质押爆仓是A股闪崩的主要触发器\n→ 剩 ~1500-1900"]
    F10 --> RESULT["最终可投资池: ~1300-2100只\n(比v1.3的1500-2500更聚焦,尾部风险更低)"]
```

**行业标准对照**（strategy-plan-v2.md §4.1）：v2.0 的十层过滤在保留 v1.3 原有的流动性+基本面过滤基础上，增加了尾部风险过滤（审计意见→财务造假预警、商誉→减值风险预警、质押→爆仓风险预警），使 Universe 更聚焦于"优质且安全"的股票。

---

## 四、因子计算全流程 [v2.0 重写]

> 来源: strategy-plan-v2.md §二 因子体系, §三 信号合成流程; tech-plan.md v2.0 §4.4

### 4.1 原始因子计算 [v2.0]

```mermaid
flowchart TD
    RAW_START["对可投资池中每只股票"] --> F1["因子1 BP+ (复合估值)\nBP = 1/PB (来源: daily_basic.pb)\nEP = 1/pe_ttm (来源: daily_basic.pe_ttm)\nCFP = 经营现金流/总市值\n合成: (z_bp+z_ep+z_cfp)/3\n⚠ 金融股(银行+非银)仅用 EP+CFP\n来源: 安信证券2024, RankICIR 3.68"]
    RAW_START --> F2["因子2 ROE = roe_yearly\n来源: fina_indicator.roe_yearly\n方向: 越大越好\n⚠ 必须用 ann_date 做 PIT 过滤"]
    RAW_START --> F3["因子3 残差MOM\nStep1: 原始动量 = P(t-21)/P(t-126) - 1\nStep2: 截面回归 → ε_i\n r_i = α + β_mkt·r_mkt + Σβ_ind·I_ind + ε_i\nStep3: 残差动量 = ε_i\n⚠ 不能用市场和行业解释的纯个股动量\n来源: GF/华泰证券2024, ICIR +0.15 (转正)"]
    RAW_START --> F4["因子4 IV60 = 1/std(日收益, 60日)\n来源: pro_bar 日收益\n方向: 越大越好"]
    RAW_START --> F5["因子5 Size = -ln(总市值)\n来源: daily_basic.total_mv\n[v2.0] log→ln变换\n方向: 越大越好(小盘溢价)"]
```

### 4.2 截面标准化 + 全因子VMP缩放 + IC引导权重合成 [v2.0 重写]

> 来源: strategy-plan-v2.md §三 信号合成流程

```mermaid
flowchart TD
    subgraph STEP2["[v2.0] 行业感知去极值 + 截面标准化"]
        direction TB
        A["Step A: [v2.0] 行业感知去极值\n申万一级行业内部分别执行\n1%/99% 分位数 Winsorize\n行业内样本<30 → 回退全市场\n目的: 保留行业间结构性差异"] --> B["Step B: 全市场 Z-score 标准化\nz = (x - 当日截面均值) / 当日截面标准差"]
        B --> C["Step C: 方向统一\n确保 z值越大 = 股票越好\n本方案5因子天然同向, 无需反转"]
    end

    RAW["[v2.0] 5个原始因子值\nBP+/ROE/残差MOM/IV60/ln Size"] --> STEP2
    STEP2 --> VMP["[v2.0] 全因子VMP波动率缩放\n对每个因子i独立计算:\nσ_i_60d = std(因子i的60日日收益) × √252\nw_vmp_i = min(2.0, 0.15 / σ_i_60d)\n上限2.0防止极端低波动过度放大\n来源: Wang & Li 2024 PBFJ"]
    
    VMP --> IC_WEIGHT["[v2.0] IC衰减引导权重\n24月滚动RankIC序列\n指数衰减: decay(t) = exp(-ln(2)×t/半衰期)\nIC_weight_i 限制在等权基线(0.2)±50%范围\n即 [0.1, 0.3]\n来源: 国泰君安2013+广发2018"]
    
    IC_WEIGHT --> STEP3["Step 3: 三维修正等权合成\nadj_weight_i = IC_weight_i × w_vmp_i\nComposite = Σ(adj_weight_i × z_i) / Σ(adj_weight_i)"]
    
    STEP3 --> WHY["设计哲学\nv1.3严格等权是防过拟合的诚实做法\nv2.0增加两维有独立学术验证的修正:\n① VMP缩放: 因子波动异常时自动降暴露\n② IC引导: 历史IC信息温和调整权重\n不涉及优化→保留等权简洁性和抗过拟合"]
    
    WHY --> DETAIL["学术来源:\nVMP: Wang & Li 2024 (A股夏普0.99→1.50)\nIC: 国泰君安2013+广发证券2018 (超额+3.7%)\n等权: Campomanes 2024 (bottom-up优于优化)"]
```

**v2.0 vs v1.3 因子流程核心差异**：

| 步骤 | v1.3 | v2.0 |
|------|------|------|
| 去极值 | 全市场 Winsorize | **行业感知** Winsorize（申万一级行业内） |
| 价值因子 | EP 单一 | **BP+ 复合** (BP+EP+CFP等权) |
| 动量因子 | 原始 6-1M 价格动量 | **残差动量**（市场+行业中性化） |
| Size | -log(总市值) | **-ln(总市值)** |
| VMP 缩放 | 仅动量, 上限=1.0 | **全5因子**, 上限=2.0 |
| 合成权重 | 等权 | **IC衰减引导** (±50%安全阀) + VMP |

### 4.3 PIT (Point-in-Time) 财务数据过滤

> 来源: strategy-plan-v2.md §4.2 关键：财务数据必须用公告日

| 报告期 | 截止日 (end_date) | 法定最晚公告日 | 使用时点 |
|--------|-------------------|---------------|---------|
| 年报 | 12-31 | 次年 4-30 | ann_date ≤ 当前交易日 |
| 一季报 | 03-31 | 当年 4-30 | ann_date ≤ 当前交易日 |
| 半年报 | 06-30 | 当年 8-31 | ann_date ≤ 当前交易日 |
| 三季报 | 09-30 | 当年 10-31 | ann_date ≤ 当前交易日 |

**示例**：2024-05-15 调仓，能用的最新 ROE 是 2024 年一季报（4月30日前公告的），不能用 2024 半年报（8月才公告）。不做此过滤，回测将"穿越"使用未来数据，结果显著高估。

---

## 五、月度调仓核心算法

> 来源: strategy-plan-v2.md §4.4-§4.6

```mermaid
flowchart TD
    START["每月第一个交易日 15:00 收盘后"] --> CALC["[v2.0] 计算所有可投资池股票的综合得分\nBP+复合估值+残差MOM+ROE+IV60+ln Size\n→ VMP缩放 → IC引导权重 → 三维修正合成"]
    CALC --> RANK["按综合得分从高到低排序"]
    RANK --> TOP10["初选 Top 10\n[v2.0] 科创板持仓 ≤ 2/10"]
    
    TOP10 --> IND_CHECK{"行业集中度检查\n申万一级行业(31个)"}
    IND_CHECK -->|"某行业 > 3只 (>30%)"| REPLACE["将该行业得分最低的\n替换为排名第11/12...的非同行业股票"]
    REPLACE --> IND_CHECK
    IND_CHECK -->|"所有行业 ≤ 30%\n科创板 ≤ 2只"| FINAL10["最终 10 只持仓"]
    
    FINAL10 --> COMPARE["对比新旧持仓 (三分类)"]
    COMPARE --> NEW["新入选且不在旧持仓\n→ 标记「买入」"]
    COMPARE --> OLD["旧持仓但不在新Top10\n→ 标记「卖出」"]
    COMPARE --> KEEP["新旧重合\n→ 持有不动"]
    
    NEW & OLD & KEEP --> ORDER["执行顺序: 先卖后买"]
    ORDER --> SELL["① 执行所有卖单\n释放现金(T+0可用)"]
    SELL --> BUY["② 总现金 ÷ 10\n= 每只买入金额"]
    BUY --> LOT["③ 按手取整 (100股=1手)\n实际手数 = round(目标金额/股价/100)\n[v2.0] 科创板成分股按200股/手"]
    
    LOT --> EXCEPTION{"处理交易异常"}
    EXCEPTION -->|"涨停买不到"| SKIP_BUY["跳过,资金分配给下一只\n排名最高的可选股票"]
    EXCEPTION -->|"跌停卖不掉"| HOLD["继续持有,次日再尝试"]
    EXCEPTION -->|"停牌"| HOLD2["跳过,继续持有或等待复牌"]
    EXCEPTION -->|"正常成交"| DONE["成交"]
    
    SKIP_BUY & HOLD & HOLD2 & DONE --> REBALANCE{"仓位权重检查\n偏离 ±3%?"}
    REBALANCE -->|"某只 >13% 或 <7%"| ADJUST["调回目标权重 10%"]
    REBALANCE -->|"偏差在 ±3% 内"| SKIP["不调整(减少成本)"]
    ADJUST & SKIP --> NEXT["下个交易日重复"]
    
    NEXT --> RISK_OVERLAY["[v2.0] 叠加仓位择时信号\n若要求50%仓位 → 只持有Top 5\n闲置资金自动申购GC001"]
```

### 按市值处理交易单位

> 来源: strategy-plan-v2.md §4.6

| 股价 | 1手金额 | 买入手数 | 实际投入 | 权重偏差 |
|------|---------|---------|---------|---------|
| 5元 | 500元 | 20手 | 10,000元 | 0% |
| 15元 | 1,500元 | 7手 | 10,500元 | +0.5% |
| 42元 | 4,200元 | 2手 | 8,400元 | -1.6% |
| 88元 | 8,800元 | 1手 | 8,800元 | -1.2% |

极端情况: 股价 > 200元 (一手 > 2万) → 直接跳过选下一只。

---

## 六、回测引擎主循环（事件驱动）[v2.0 Walk-Forward]

> 来源: tech-plan.md v2.0 §4.6 backtest.py, §5.1-§5.4; strategy-plan-v2.md §六 Phase 3

### 6.1 单次回测主循环

```mermaid
flowchart TD
    START["回测开始: start_date → end_date"] --> LOOP["遍历每个交易日 t"]
    
    LOOP --> IS_REBALANCE{"今天是否调仓日?\n(每月第一个交易日)"}
    
    IS_REBALANCE -->|否| SKIP["跳过调仓"]
    
    IS_REBALANCE -->|是| GET_UNIVERSE["a. 获取当日 universe\n← universe.py.build(date)\n[v2.0] 十层过滤"]
    GET_UNIVERSE --> GET_FACTORS["b. [v2.0] 获取因子综合得分\n← factors.py.compute_all_dates()\nBP+/残差MOM/ROE/IV60/ln Size\n+ 行业感知去极值 + VMP缩放 + IC引导\n⚠ 所有信号基于 t-1 数据"]
    GET_FACTORS --> SELECT["c. 选 Top N 目标持仓\n← portfolio.py.select_top_n()\n[v2.0] 科创板≤2/10"]
    SELECT --> GEN_ORDERS["d. 生成订单\n← portfolio.py.generate_orders()"]
    GEN_ORDERS --> EXECUTE["e. 执行订单 (D+1日开盘价)"]
    
    subgraph EXEC_DETAIL["订单执行细节"]
        direction TB
        E1["检查涨停: BUY无法成交 → 跳过"]
        E2["检查跌停: SELL无法成交 → 次日重试"]
        E3["检查停牌: 无法交易 → 跳过"]
        E4["手数取整: round(目标金额/股价/100) × 100"]
        E5["成本扣除: 佣金(万2.5,最低5元)+印花税(万5,卖出)+过户费(万0.1,沪市)+滑点(10bps)"]
    end
    
    EXECUTE --> UPDATE["f. 更新持仓"]
    UPDATE & SKIP --> VALUATION["3. 估值: 按当日收盘价计算持仓市值"]
    VALUATION --> RECORD["4. 记录每日净值"]
    RECORD --> CHECK_RISK["5. 检查风控信号"]
    
    subgraph RISK_CHECK["风控检查 [v2.0]"]
        direction TB
        R1["[v2.0] 仓位择时:\n条件A(中证500<MA20) AND 条件B(σ_20d>σ_median_60d)\nAND 连续5日确认 → 仓位降至50%\n恢复: 条件A不成立 AND 连续3日确认 → 满仓"]
        R2["[v2.0] 熔断检查:\nalpha回撤<-10%/绝对回撤>15%"]
    end
    
    CHECK_RISK --> NEXT_DAY["t = t + 1"]
    NEXT_DAY -->|"t ≤ end_date"| LOOP
    NEXT_DAY -->|"t > end_date"| DONE["回测完成 → BacktestResult"]
```

### 6.2 [v2.0] Walk-Forward 滚动回测框架

```mermaid
flowchart TD
    START["Walk-Forward 回测\n全区间: 2015-01 至 2025-12"] --> W1["窗口1: Train 2015-2017 → Test 2018"]
    W1 --> W2["窗口2: Train 2016-2018 → Test 2019"]
    W2 --> W3["窗口3: Train 2017-2019 → Test 2020"]
    W3 --> W4["窗口4: Train 2018-2020 → Test 2021"]
    W4 --> W5["窗口5: Train 2019-2021 → Test 2022"]
    W5 --> W6["窗口6: Train 2020-2022 → Test 2023"]
    W6 --> W7["窗口7: Train 2021-2023 → Test 2024"]
    W7 --> W8["窗口8: Train 2022-2024 → Test 2025"]
    
    W8 --> COLLECT["收集8条样本外路径"]
    COLLECT --> PBO["PBO 计算 (pypbo)\n过拟合概率\n目标: < 30%"]
    COLLECT --> DSR["DSR 计算\nDeflated Sharpe Ratio\n目标: > 1.0"]
    COLLECT --> SUMMARY["样本外路径汇总\n≥80%路径正超额 → 合格"]
    
    PBO & DSR & SUMMARY --> VERDICT{"三项全通过?"}
    VERDICT -->|是| PASS["v2.0 策略通过过拟合检验"]
    VERDICT -->|否| REVIEW["简化参数/减少因子\n→ 重新验证"]
```

### T+1 制度执行时序

> 来源: tech-plan.md v2.0 §5.2

```
D日 15:00  收盘 → [v2.0] 计算BP+/残差MOM/ROE/IV60/ln Size → VMP缩放 + IC引导 → 综合得分 → 选出新持仓 → 生成订单
D+1日 09:30 开盘 → 执行订单(以开盘价成交)
     ├─ 先执行所有卖单 → 释放现金
     │   资金 T+0 可用 (当日可买入), T+1 可取
     └─ 再执行买单 → 用总现金 ÷ 10 计算每只金额
D+1日 15:00 收盘 → 新持仓生效
```

---

## 七、风险管理三层架构 [v2.0]

> 来源: strategy-plan-v2.md §五 风险管理体系

```mermaid
flowchart TD
    subgraph L1["第一层: 组合层面风控 (portfolio.py)"]
        direction TB
        L1A["单只股票最大权重: 10%\n(等权时天然满足)"]
        L1B["单一行业最大暴露: 30%\n(超过时替换为下一排名股票)"]
        L1C["[v2.0] 科创板最大持仓: 2/10 (20%)\n(放宽原完全排除)"]
        L1D["[v2.0] 现金管理: 降仓闲置资金自动申购GC001\n年化增收~1%"]
    end
    
    subgraph L2["第二层: [v2.0] 市场择时叠加 (risk.py)"]
        direction TB
        L2A["[v2.0] 双条件触发(参数校准版):\n条件A: 中证500 < MA20 (长城2016)\n条件B: σ_20d > σ_median_60d\n降仓 = A AND B AND 连续5日确认\n→ 总仓位降至 50%\n(持有5只而非10只)\n其余现金 → GC001国债逆回购"]
        L2B["[v2.0] 恢复满仓:\n条件A不成立 AND 连续3日确认\n(华泰金工2026: 拐点识别率仅61.8%)\n→ 总仓位恢复至 100%"]
        L2C["效果: 熊市降低回撤\nMA20对中证500趋势反转响应更快\n恢复迟滞防止假反弹过早入场\n来源: 长城2016+国信2018+华泰2026"]
    end
    
    subgraph L3["第三层: [v2.0] 熔断机制 (risk.py)"]
        direction TB
        L3A["熔断条件1 (alpha回撤)\n滚动6月超额收益 < -10%\n→ 暂停策略,全部清仓\n→ 人工复核后决定"]
        L3B["熔断条件2 (早期预警)\n连续3月单月超额 < -3%\n→ 标记「关注」不出手\n→ 连续2月正常后解除"]
        L3C["[v2.0] 熔断条件3 (硬熔断)\n绝对回撤 > 15% (从实盘高点)\n(原25%→15%,小账户保护)\n→ 暂停策略,全部清仓\n→ 人工复核后决定"]
    end
    
    L1 --> L2 --> L3
```

**设计原则**（strategy-plan-v2.md §5.1）：区分 beta 回撤（市场系统性下跌）和 alpha 回撤（策略选股失效）。市场跌但超额为正 → 策略正常，不应熔断。极端系统性风险 → 硬熔断保护本金。[v2.0] 15% 硬熔断对小账户（10万）更合理——亏15%仅1.5万，亏25%需赚33%才能回本。

---

## 八、因子健康度持续监控 [v2.0]

> 来源: strategy-plan-v2.md §六 Phase 5

```mermaid
flowchart TD
    START["实盘运行中"] --> M1["指标1: 单因子滚动12月 Rank IC\n预警: < 0 (转负)\n频率: 月度"]
    START --> M2["指标2: 单因子滚动12月 IC t-stat\n预警: < 1.5\n频率: 月度"]
    START --> M3["指标3: 单因子多头组合滚动6月超额\n预警: < -5%\n频率: 周度"]
    START --> M4["指标4: 综合得分滚动6月 Rank IC\n预警: < 0.02\n频率: 月度"]
    START --> M5["指标5: 月度换手率\n预警: > 50%(异常升高)\n频率: 月度"]
    START --> M6["[v2.0] 指标6: 全5因子60日滚动波动率\nσ_i_60d = std(因子i日收益,60)×√252\n预警: 任一因子 > 历史90%分位数\n频率: 周度\n来源: Wang & Li 2024"]
    START --> M7["[v2.0] 指标7: 因子分散化比率\nDR = 5 / Σ(两两相关系数)\n预警: DR < 2.0(平均相关>0.5)\n频率: 月度\n来源: 招商证券 2020"]
    START --> M8["[v2.0] 新增] 指标8: 因子拥挤度评分\n多维度指标（估值/动量/波动率偏离度）\n预警: > 80%分位数\n频率: 季度\n来源: Ping'an Securities 2024"]
    
    M1 & M2 & M3 & M4 & M5 & M6 & M7 & M8 --> CHECK{"任一指标触发预警?"}
    CHECK -->|否| CONTINUE["继续正常运行"]
    CHECK -->|是| MARK["标记「需关注」\n不自动停策略"]
    MARK --> COUNT{"连续3个月预警?"}
    COUNT -->|否| CONTINUE
    COUNT -->|是| REVIEW["人工复核:\n是否调整因子权重\n或剔除失效因子"]
    
    CONTINUE --> START
    REVIEW --> START
```

---

## 九、防过拟合体系 [v2.0 扩展]

> 来源: strategy-plan-v2.md §七 防过拟合措施

```mermaid
flowchart LR
    A["1. [v2.0] Walk-Forward\n滚动回测\n多路径样本外验证"] --> H["8项措施"]
    B["2. [v2.0] PBO + DSR\n统计检验\n过拟合概率量化"] --> H
    C["3. 因子数量少\n5个核心+最多1个实验性\n每个有独立经济学逻辑"] --> H
    D["4. 权重有安全阀\nIC权重±50%限制\nVMP缩放上限2.0"] --> H
    E["5. 参数敏感性分析\nv2.0新增参数全网格搜索"] --> H
    F["6. 纸面交易验证\n2-4周免费试错期"] --> H
    G["7. 实盘渐进入场\n30%→60%→100%"] --> H
    H2["8. [v2.0] PBO滚动更新\n每季度用新数据重算PBO\n监控策略是否在实盘中过拟合"] --> H
    
    H --> OUTCOME["[v2.0] 验收标准:\nPBO < 30%\nDSR > 1.0\nWF≥80%路径正超额\n参数±20%变化后超额变化 < 25%\n← tech-plan.md Phase 3 验收标准"]
```

---

## 交叉验证清单

| 流程图 | 来源文档 | 关键章节 |
|--------|---------|---------|
| 一、系统总体架构 | tech-plan.md v2.0 | §2.1, §2.2 |
| 二、数据下载全流程 | tech-plan.md v2.0 | §1.2, §1.3, §3.1-§3.4 |
| 三、可投资池构建（十层过滤） | strategy-plan-v2.md v2.0 | §4.1 |
| 四、因子计算全流程（BP+/残差MOM/VMP全扩展/IC引导） | strategy-plan-v2.md v2.0 §二, §三 + tech-plan.md v2.0 §4.4 | — |
| 五、月度调仓核心算法 | strategy-plan-v2.md v2.0 | §4.4-§4.6 |
| 六、回测引擎（MA20 + Walk-Forward） | tech-plan.md v2.0 §4.6, §5.1-§5.4 + strategy-plan-v2.md §六 Phase 3 | — |
| 七、风险管理三层架构（MA20 + 恢复迟滞 + 15%熔断） | strategy-plan-v2.md v2.0 | §5.1-§5.2 |
| 八、因子健康度监控（+拥挤度指标） | strategy-plan-v2.md v2.0 | §六 Phase 5 |
| 九、防过拟合体系（WF + PBO + DSR） | strategy-plan-v2.md v2.0 §七 + tech-plan.md v2.0 §七 | — |

---

## v2.0 参数速查表

| 参数 | v1.3 值 | v2.0 值 | 所属模块 | 来源 |
|------|--------|--------|---------|------|
| `sigma_target` | 0.15（仅动量） | 0.15（全部因子） | `config.py` → `factors.py` | Wang & Li (2024) |
| `mom_lookback` | 60 | 60 | `config.py` → `factors.py` | Moreira & Muir (2017) |
| `vmp_upper_bound` | 1.0 | **2.0** | `config.py` → `factors.py` | v2.0 新增 |
| `ic_lookback_months` | — | **24** | `config.py` → `factors.py` | 国泰君安 (2013) |
| `ic_half_life_months` | — | **6** | `config.py` → `factors.py` | 中信建投 (2019) |
| `ic_weight_deviation_max` | — | **0.5** | `config.py` → `factors.py` | v2.0 安全阀 |
| `ma_period` | 60 | **20** | `config.py` → `risk.py` | 长城证券 (2016) |
| `vol_window` | 20 | 20 | `config.py` → `risk.py` | 国信证券 (2018) |
| `vol_percentile` | 0.50 | 0.50 | `config.py` → `risk.py` | 长城证券 (2016) |
| `hysteresis_down` | 5 | 5 | `config.py` → `risk.py` | 行业惯例 |
| `hysteresis_up` | —（立即） | **3** | `config.py` → `risk.py` | 华泰金工 (2026) |
| `hard_drawdown_limit` | -0.25 | **-0.15** | `config.py` → `risk.py` | 小账户保护 |
| `max_star_market_weight` | 0（完全排除） | **2** | `config.py` → `portfolio.py` | v2.0 放宽 |
| `walk_forward_train_years` | —（单次分割） | **3** | `config.py` → `backtest.py` | Lopez de Prado (2018) |
| `walk_forward_test_years` | — | **1** | `config.py` → `backtest.py` | Lopez de Prado (2018) |
| `pbo_threshold` | — | **0.30** | `config.py` → `validation.py` | Joubert et al. (2024) |
| `enable_gc001` | — | **true** | `config.py` → `portfolio.py` | v2.0 新增 |
