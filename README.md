# Amazon Growth Attribution

亚马逊订单来源与增长归因项目，用于整合订单、广告、站内促销、站外推广和价格数据，建立可追溯、可复跑、可审计的订单来源分析体系。

## 项目状态

**V1.0 已完成。**

当前已经形成两条可用的数据链路：

1. **店铺 × 月份的五类订单来源结构**；
2. **款号 × 月份的销量加权成交价**。

V2.0 将在取得稳定的 SKU/MSKU 级来源数据后，继续推进：

```text
店铺 × 月份 × 款号 × 来源类型
```

详细成果、验收快照、已知限制和 V2.0 路线图见：

- [V1.0 成果总结](docs/v1_0_summary.md)
- [订单来源归因规则](docs/order_attribution_rules.md)

## V1.0 解决的问题

### 1. Amazon All Orders 原始数据入库

已支持：

- Seller Central All Orders Report TXT 流式/分批入库；
- UTF-8 BOM、制表符和动态表头识别；
- 订单商品唯一键和幂等处理；
- 来源文件、SHA-256、源行号、导入批次审计；
- 目录批量导入；
- 未完结月份月累计文件整月替换；
- `RKZ2607-.txt` 等特殊文件名解析；
- `quantity=0` 原始记录保留；
- 数据库异常重试和事务回滚。

### 2. 五类互斥订单来源

最终分类优先级：

```text
站外推广 > 广告 > 站内促销 > 低价 > 自然
```

其中：

- 站外推广、站内促销和低价主要由订单及促销字段识别；
- 广告在缺少 Amazon 订单号级来源时采用可复现的统计分配；
- 原始标签和最终互斥分类同时保留；
- 广告分配保留层级、置信度、规则版本和月度审计。

当前五类结果适合用于**店铺 × 月份**的来源结构分析，不代表 Amazon 官方逐单广告归因。

### 3. 款号月度成交价

已打通以下关联链路：

```text
订单 store_name + MSKU
→ Listing 店铺 + MSKU
→ Listing 本地 SKU + 当前负责人
→ 产品管理快照 SKU
→ SPU/款号 + 季节 + 品类
```

成交价口径：

```text
销量加权成交价
= SUM(item_price - item_promotion_discount) / SUM(quantity)
```

支持综合版和按店铺拆分版 Excel，店铺包括：

- `JQ-US`
- `MT-US`
- `RKZ-US`
- `SY-US`

## 数据口径

### 有效订单

五类归因和成交价统计只处理满足以下条件的订单商品明细：

```text
sales_channel = Amazon.com
order_status = Shipped
item_status = Shipped
quantity > 0
item_price > 0
currency = USD
SKU 非空
purchase_date_utc 非空
```

订单月份以 `purchase_date_utc` 从 UTC 转换为 `America/Los_Angeles` 后归属。

### 成交价

只计算商品本身的净成交金额：

```text
item_price - item_promotion_discount
```

不包含买家运费、税费、退款、平台费、FBA费和广告费。

## 数据流程

```text
Amazon All Orders TXT
        │
        ▼
ods_amz_all_orders_report
        │
        ├──────────────► 有效订单商品基础层
        │                       │
        │                       ▼
        │               五类互斥来源结果
        │                       │
        │                       ▼
        │               店铺 × 月份来源汇总
        │
        └──► Listing ──► 本地SKU ──► 产品管理快照
                                    │
                                    ▼
                           款号 × 月份成交价
```

## 核心结果表

- `dwd_amz_order_attribution_base`：有效订单商品归因基础表；
- `dws_amz_ad_order_demand_daily`：广告订单统计需求；
- `dwd_amz_ad_order_allocation`：广告订单分配结果；
- `dwd_amz_order_attribution_result`：五类互斥归因结果；
- `dws_amz_order_source_monthly`：店铺月度五类来源汇总；
- 广告分配月度审计表。

## 项目结构

```text
amazon-growth-attribution/
├─ pipelines/
│  ├─ orders/                 # All Orders基础导入、建表及索引
│  └─ attribution/            # 五类归因、月度回填和广告统计分配
├─ imports/
│  ├─ import_amazon_orders_txt_monthly.py
│  └─ import_amazon_orders_txt_monthly_v2.py
├─ reports/
│  ├─ export_style_monthly_deal_price.py
│  └─ export_style_monthly_deal_price_by_store.py
├─ docs/
│  ├─ order_attribution_rules.md
│  └─ v1_0_summary.md
├─ .env.example
├─ .gitignore
├─ requirements.txt
└─ README.md
```

`reports/` 目录还包含五类订单月度汇总和审计结果的 Excel 导出脚本。

## 快速开始

### 1. 安装环境

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

生产服务器可直接加载现有环境变量文件：

```bash
source /root/lingxing_env.sh
```

### 2. 预检未完结月份 TXT

```bash
python imports/import_amazon_orders_txt_monthly_v2.py \
  imports/data/JQ2607-.txt
```

预检默认不修改数据库。

### 3. 整月替换导入

```bash
python imports/import_amazon_orders_txt_monthly_v2.py \
  imports/data/JQ2607-.txt \
  --apply \
  --replace-month
```

未完结月份文件为月累计数据，禁止直接追加。重新下载最新文件后，应再次执行整月替换。

### 4. 导出款号月度成交价综合版

```bash
DEAL_PRICE_START_MONTH='2025-01-01' \
DEAL_PRICE_END_MONTH_EXCLUSIVE='2026-08-01' \
DEAL_PRICE_STORES='JQ-US,MT-US,RKZ-US,SY-US' \
python reports/export_style_monthly_deal_price.py
```

### 5. 按店铺拆分导出

```bash
DEAL_PRICE_START_MONTH='2025-01-01' \
DEAL_PRICE_END_MONTH_EXCLUSIVE='2026-08-01' \
DEAL_PRICE_STORES='JQ-US,MT-US,RKZ-US,SY-US' \
python reports/export_style_monthly_deal_price_by_store.py
```

输出文件位于 `exports/`，该目录中的真实业务结果不得提交到仓库。

## V1.0 已知限制

### 广告不是官方逐单归因

普通广告数据缺少 Amazon 订单号级关联，因此广告订单属于统计分配结果。必须结合分配层级、置信度和审计结果使用。

### 款号成交价不等于款号来源归因

当前已经完成“款号 × 月份”的成交价、销量和基础维度，但还没有获得足够稳定的 SKU/MSKU 级来源数据，因此不能把店铺月度来源总量直接解释为款号来源结构。

### 负责人是当前快照

负责人读取 Listing 当前负责人，不代表历史月份当时的负责人。

### 映射完整性依赖主数据维护

未匹配 Listing、本地 SKU、产品管理快照、SPU、季节、品类或负责人时，会进入映射审计，不应静默丢弃。

## V2.0 路线图

V2.0 的前提是取得稳定、可审计的 SKU/MSKU 级来源数据。

推荐目标粒度：

```text
店铺 × 月份 × 款号 × 来源类型
```

计划指标：

- 订单-SKU数；
- 销量；
- 净商品销售额；
- 来源占比；
- 销量加权成交价；
- MSKU数、本地SKU数；
- 分配层级、置信度和完整率；
- 季节、品类和负责人。

V2.0 不应通过扩大当前月度统计分配来强行生成款号归因，而应先完成 SKU/MSKU 级来源数据采集、统一键建设和质量审计。

## 安全说明

仓库只保存代码、SQL、文档和示例配置。以下内容禁止提交：

- `.env`、数据库密码、Token和服务器密钥；
- Amazon TXT、CSV、XLSX原始报告；
- 导入日志、失败数据和导出结果；
- 包含买家地址、身份字段或其他真实PII的数据。

当前仓库为公开仓库。代码本身不应包含真实数据或密钥；若未来加入内部规则、真实字段样例、业务验收数据明细或管理口径，建议切换为私有仓库。
