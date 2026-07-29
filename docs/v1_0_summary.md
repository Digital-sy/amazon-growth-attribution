# Amazon Growth Attribution V1.0 成果总结

> 状态：V1.0 阶段完结  
> 当前规则：低价净成交单价 ≤ 10 USD  
> 站内促销：仅作展示标签，不参与来源合计和占比  
> 当前规则版本：`v5_10usd_promo_overlay_20260729`  
> 下一阶段：取得稳定的 SKU/MSKU 级来源数据后，推进“款号 × 月份”归因分析。

## 1. 项目目标

本项目用于整合 Amazon 订单、广告、站内促销、站外推广和价格数据，建立可追溯、可复跑、可审计的订单来源分析体系。

V1.0完成：

1. Amazon All Orders Report稳定入库；
2. 有效订单商品明细统一；
3. 四个互斥主来源识别；
4. 站内促销重叠展示；
5. 店铺月度、季度、半年度汇总；
6. 广告统计分配与审计；
7. 款号月度成交价和商品维度映射。

## 2. 来源结构

业务展示五类：

```text
广告
站外推广
站内促销
低价
自然
```

### 2.1 四个主来源

```text
站外推广 > 广告 > 低价 > 自然
```

广告、站外推广、低价、自然互斥，可用于：

- 订单合计；
- 销量合计；
- 销售额合计；
- 订单占比；
- 销售额占比。

四个主来源占比合计为100%。

### 2.2 站内促销展示标签

站内促销不再作为互斥主来源，而是重叠展示项：

- 可以与广告或自然重叠；
- 展示订单量、销量和销售额；
- 不参与来源合计；
- 不参与订单占比和销售额占比；
- 未归入广告或站外的站内促销订单，其主来源计入自然。

## 3. 低价规则

商品净成交单价：

```text
(item_price - item_promotion_discount) / quantity
```

低价只在排除站外推广、广告和站内促销后识别：

```text
净成交单价 <= 10 USD → 低价
净成交单价 > 10 USD  → 自然
```

站内促销订单即使净成交单价不高于10美元，也不进入低价主来源；其主来源按广告或自然处理。

## 4. Amazon All Orders TXT 入库

已支持：

- UTF-8 BOM、制表符和动态表头识别；
- 文件名自动识别店铺和月份；
- 未完结月份月累计文件整月替换；
- 来源文件 SHA-256、源行号和导入批次审计；
- `quantity=0` 原始记录保留；
- 原始或合成 `order_item_id` 唯一性保障；
- 事务回滚和数据库异常重试。

核心脚本：

- `pipelines/orders/import_amazon_all_orders.py`
- `imports/import_amazon_orders_txt_monthly.py`
- `imports/import_amazon_orders_txt_monthly_v2.py`

## 5. 有效订单基础层

有效订单口径：

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

## 6. 广告统计分配与审计

Amazon普通广告数据没有订单号级真实关联，因此广告订单属于可复现的统计分配结果。

生产口径使用产品表现月度 `ad_order_quantity`，按店铺+月份从非站外订单池中分配，并保留：

- 广告目标；
- 候选订单；
- 已分配和未分配数量；
- 分配率；
- 分配层级；
- 置信度；
- 规则版本；
- 月度审计结果。

广告候选池可以包含站内促销订单。命中广告分配后，主来源为广告，同时可保留站内促销展示标签。

## 7. 月度、季度和半年度

基础事实表按月份保存，报表支持：

```text
月度：YYYY-MM
季度：Q1=1-3月，Q2=4-6月，Q3=7-9月，Q4=10-12月
半年度：上半年=1-6月，下半年=7-12月
```

季度和半年度由月度结果汇总，不单独维护另一套事实数据。

所有周期中：

- 合计和占比只使用四个主来源；
- 站内促销只展示；
- 默认只输出完整季度和完整半年度。

## 8. 完整来源工作簿

主脚本：

```text
reports/export_order_source_summary.py
```

工作簿包含：

```text
总览
月度汇总
季度汇总
半年度汇总
店铺月度明细
广告审计
口径说明
```

站内促销行标记为“展示项”，占比显示为“—”。堆积图只展示广告、站外推广、低价、自然。

## 9. 核心数据表

- `dwd_amz_order_attribution_base`：有效订单商品基础层；
- `dwd_amz_ad_order_allocation`：广告统计分配结果；
- `dwd_amz_order_attribution_result`：订单商品主来源及标签；
- `dws_amz_order_source_monthly`：四个主来源行加一个站内促销展示行；
- `dws_amz_product_performance_ad_monthly_audit`：广告分配审计。

## 10. 款号月度成交价

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

已支持综合版和按 `JQ-US`、`MT-US`、`RKZ-US`、`SY-US` 四店拆分版Excel。

## 11. 生产入口

```text
pipelines/attribution/run_attribution_pipeline.sh
```

正式入口调用：

```text
run_attribution_pipeline_10usd_promo_overlay.sh
```

处理流程：

1. 执行产品表现月度广告统计分配；
2. 按10美元重算低价；
3. 将站内促销从互斥主来源中移出；
4. 将未归入广告/站外的站内促销订单计入自然；
5. 生成站内促销展示行；
6. 重建月度来源汇总。

## 12. V1.0 边界

### 已可靠支持

- Amazon订单入库与审计；
- 有效订单口径统一；
- 店铺月度、季度和半年度主来源结构；
- 站内促销展示指标；
- 来源订单量、销量、销售额和占比；
- 广告统计分配过程及审计；
- 款号月度销量加权成交价。

### 当前不能宣称

- Amazon官方逐单广告归因；
- SKU/款号层面的精准来源归因；
- 站内促销可与四个主来源直接相加；
- 季度或半年度比月度具有更高归因精度；
- 当前负责人代表历史月份负责人。

## 13. V2.0 路线图

V2.0前提是取得稳定、可审计的SKU/MSKU级来源数据。

目标粒度：

```text
店铺 × 月份 × 款号 × 来源类型
```

推荐顺序：

1. 固化SKU/MSKU级来源采集；
2. 建立统一键和质量审计；
3. 校验SKU级来源与店铺月度总量；
4. 关联Listing、本地SKU和产品管理快照；
5. 生成款号×月份×来源类型结果；
6. 建立未匹配、重复映射和分配差额审计。