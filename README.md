# Amazon Growth Attribution

亚马逊订单来源与增长归因项目，用于整合订单、广告、站内促销、站外推广和价格数据，建立可追溯、可复跑、可审计的订单来源分析体系。

## 项目状态

**V1.0 已完成。**

当前形成两条可用的数据链路：

1. **店铺 × 月份/季度/半年度的来源结构**；
2. **款号 × 月份的销量加权成交价**。

V2.0 将在取得稳定的 SKU/MSKU 级来源数据后，继续推进：

```text
店铺 × 月份 × 款号 × 来源类型
```

详细文档：

- [V1.0 成果总结](docs/v1_0_summary.md)
- [订单来源归因规则](docs/order_attribution_rules.md)

## 当前五类口径

业务仍展示五类：

```text
广告
站外推广
站内促销
低价
自然
```

但五类分为两种指标角色。

### 四个主来源

```text
站外推广 > 广告 > 低价 > 自然
```

广告、站外推广、低价、自然互斥：

- 参与订单合计；
- 参与销售额合计；
- 参与订单占比；
- 参与销售额占比；
- 四类占比合计为100%。

### 站内促销展示项

站内促销是可重叠标签，不再作为互斥主来源：

- 可以与广告或自然重叠；
- 展示订单量、销量和销售额；
- 不参与来源合计；
- 不参与订单占比和销售额占比；
- 未归入广告或站外的站内促销订单，其主来源计入自然。

## 低价口径

低价恢复为 **10美元**。

```text
净成交单价
= (item_price - item_promotion_discount) / quantity
```

只有排除站外推广、广告和站内促销后，才判断低价：

```text
净成交单价 <= 10 USD → 低价
净成交单价 > 10 USD  → 自然
```

当前生产规则版本：

```text
v5_10usd_promo_overlay_20260729
```

历史 `v4_7usd_20260728` 结果需要重新运行正式归因入口，才能切换到当前口径。

## V1.0 完成范围

### 1. Amazon All Orders 原始数据入库

已支持：

- Seller Central All Orders Report TXT 流式/分批入库；
- UTF-8 BOM、制表符和动态表头识别；
- 订单商品唯一键和幂等处理；
- 来源文件、SHA-256、源行号和导入批次审计；
- 未完结月份月累计文件整月替换；
- `quantity=0` 原始记录保留；
- 数据库异常重试和事务回滚。

### 2. 广告统计分配

Amazon普通广告数据缺少订单号级真实关联，因此广告订单属于可复现的统计分配结果。

生产口径使用产品表现月度 `ad_order_quantity`，按店铺+月份从非站外订单池中分配，并保留：

- 分配目标；
- 候选订单；
- 已分配和未分配数量；
- 分配率；
- 分配层级；
- 置信度；
- 规则版本；
- 月度审计。

该结果适合用于店铺月度、季度和半年度来源结构分析，不代表 Amazon 官方逐单广告归因。

### 3. 月度、季度和半年度

来源事实表按月保存，季度和半年度由月度结果动态汇总，不重复维护另一套事实数据。

```text
月度：YYYY-MM
季度：Q1=1-3月，Q2=4-6月，Q3=7-9月，Q4=10-12月
半年度：上半年=1-6月，下半年=7-12月
```

默认只输出完整季度和完整半年度。设置 `ATTR_INCLUDE_PARTIAL_PERIODS=1` 可输出未完结周期。

### 4. 完整来源 Excel

主导出脚本：

```text
reports/export_order_source_summary.py
```

一个Excel包含：

```text
总览
月度汇总
季度汇总
半年度汇总
店铺月度明细
广告审计
口径说明
```

报表规则：

- 保留五类展示；
- 站内促销行标记为“展示项”；
- 站内促销的订单占比和销售额占比显示为“—”；
- 合计、分母和堆积图只使用广告、站外推广、低价、自然四个主来源。

### 5. 款号月度成交价

关联链路：

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

支持综合版和按店铺拆分版Excel：

- `JQ-US`
- `MT-US`
- `RKZ-US`
- `SY-US`

## 有效订单范围

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
        │               四个互斥主来源
        │                       │
        │                       ├──► 站内促销展示标签
        │                       │
        │                       ▼
        │               店铺 × 月份来源汇总
        │                       │
        │                       ├──► 季度汇总
        │                       └──► 半年度汇总
        │
        └──► Listing ──► 本地SKU ──► 产品管理快照
                                    │
                                    ▼
                           款号 × 月份成交价
```

## 核心结果表

- `dwd_amz_order_attribution_base`：有效订单商品归因基础表；
- `dwd_amz_ad_order_allocation`：广告订单统计分配结果；
- `dwd_amz_order_attribution_result`：订单商品主来源和标签结果；
- `dws_amz_order_source_monthly`：店铺月度来源汇总，含四个主来源行和一个站内促销展示行；
- `dws_amz_product_performance_ad_monthly_audit`：广告分配月度审计。

## 项目结构

```text
amazon-growth-attribution/
├─ pipelines/
│  ├─ orders/
│  └─ attribution/
│     ├─ run_attribution_pipeline.sh
│     ├─ run_attribution_pipeline_10usd_promo_overlay.sh
│     └─ ...
├─ imports/
│  ├─ import_amazon_orders_txt_monthly.py
│  └─ import_amazon_orders_txt_monthly_v2.py
├─ reports/
│  ├─ export_order_source_summary.py
│  ├─ export_order_source_period_summary.py
│  ├─ export_style_monthly_deal_price.py
│  └─ export_style_monthly_deal_price_by_store.py
├─ docs/
│  ├─ order_attribution_rules.md
│  └─ v1_0_summary.md
└─ README.md
```

## 快速开始

### 1. 拉取并加载环境

```bash
cd /data/bi_scripts/amazon-growth-attribution
git pull origin main
source /root/lingxing_env.sh
```

### 2. 按10美元+站内促销展示口径重跑

```bash
ATTR_START_MONTH='2025-01-01' \
ATTR_END_MONTH_EXCLUSIVE='2026-08-01' \
ATTR_STORES='JQ-US,MT-US,RKZ-US,SY-US' \
bash pipelines/attribution/run_attribution_pipeline.sh
```

正式入口会：

1. 执行产品表现月度广告分配；
2. 将低价阈值恢复为10美元；
3. 将未归入广告/站外的站内促销订单计入自然主来源；
4. 生成站内促销重叠展示行；
5. 重建月度来源汇总。

### 3. 导出完整工作簿

```bash
ATTR_EXPORT_START_MONTH='2025-01-01' \
ATTR_EXPORT_END_MONTH_EXCLUSIVE='2026-08-01' \
ATTR_STORES='JQ-US,MT-US,RKZ-US,SY-US' \
/data/venvs/bi_venv/bin/python \
  reports/export_order_source_summary.py
```

输出：

```text
exports/amazon_order_source_summary_202501_202607.xlsx
```

### 4. 验证规则版本

```sql
SELECT
    rule_version,
    COUNT(*) AS item_rows,
    MIN(order_month) AS min_month,
    MAX(order_month) AS max_month
FROM dwd_amz_order_attribution_result
WHERE order_month >= '2025-01-01'
  AND order_month <  '2026-08-01'
GROUP BY rule_version;
```

新结果应显示：

```text
v5_10usd_promo_overlay_20260729
```

## V1.0 已知限制

- 广告属于统计分配，不代表 Amazon 官方逐单广告归因；
- 站内促销是重叠展示标签，不能与四个主来源相加；
- 季度和半年度只是月度结果汇总，不会提高归因精度；
- 当前尚不支持可验证的款号级来源归因；
- 负责人来自当前Listing快照，不代表历史月份负责人。

## V2.0 路线图

V2.0前提是取得稳定、可审计的SKU/MSKU级来源数据。

目标粒度：

```text
店铺 × 月份 × 款号 × 来源类型
```

计划指标包括订单-SKU数、销量、净商品销售额、来源占比、销量加权成交价、数据完整率、季节、品类和负责人。