# 订单来源归因规则

## 1. 分类目标

最终订单来源采用互斥优先级：

```text
站外推广 > 广告 > 站内促销 > 低价 > 自然
```

先在订单商品明细层保留原始标签，再生成最终主分类。

## 2. 有效数据范围

订单分类只处理满足以下条件的商品明细：

```text
sales_channel = Amazon.com
order_status = Shipped
item_status = Shipped
quantity > 0
item_price > 0
currency = USD
```

取消、Pending、Non-Amazon、多渠道配送及零金额疑似换货或补发订单不进入五分类。

## 3. 站外推广

当 `promotion_ids` 中任意促销ID以 `MPC-` 开头时，标记为站外推广。

同一商品行即使同时出现普通 Percentage Off，也由站外优先。

## 4. 广告

Amazon普通广告报告没有订单号级归因，因此广告订单属于估算结果。

V1.0 当前生产口径使用产品表现月度 `ad_order_quantity`，按店铺+月份进行可复现统计分配。

广告候选池为全部非站外有效订单，不能提前排除站内促销订单，因为广告和Coupon、Deal可能重合。

分配结果必须保留：

- `estimated_ad_flag`
- 分配粒度
- 分配置信度
- 分配批次/规则版本
- 广告归因数量来源
- 分类依据

## 5. 站内促销

未归入站外或广告，并满足以下任一条件：

- `promotion_ids` 包含已识别的站内商品促销；
- `promotion_ids` 命中 Percentage Off 或 PLM；
- `item_promotion_discount > 0`。

`ship_promotion_discount` 仅表示配送优惠，不能单独判断为站内商品促销。

## 6. 低价

未归入站外推广、广告或站内促销，且商品净成交单价不高于 **7美元**：

```text
商品净成交单价 = (item_price - item_promotion_discount) / quantity
低价条件 = 商品净成交单价 <= 7 USD
```

不扣除税费、买家运费或配送优惠。

规则版本从 `v4_7usd_20260728` 起统一使用7美元阈值。历史按10美元生成的结果需要重新运行归因入口后才能与新口径一致。

## 7. 自然

所有剩余有效订单商品明细归为自然订单，包括净成交单价高于7美元且未命中前三类的订单。

## 8. 统计周期

基础事实表仍按月份保存，汇总层支持：

- 月度；
- 季度：Q1=1-3月、Q2=4-6月、Q3=7-9月、Q4=10-12月；
- 半年度：上半年=1-6月、下半年=7-12月。

默认只输出完整季度和完整半年度。例如数据截至2026年7月时，完整周期包括：

- 2025-Q1、Q2、Q3、Q4；
- 2026-Q1、Q2；
- 2025上半年、2025下半年；
- 2026上半年。

未完结的2026-Q3和2026下半年默认不纳入；需要时可显式开启未完结周期输出。

## 9. 推荐保留字段

```text
offsite_flag
onsite_promotion_flag
low_price_flag
estimated_ad_flag
main_order_type
classification_reason
ad_allocation_level
ad_allocation_confidence
rule_version
```

站外、促销、低价为规则识别；广告为统计估算，两者不能混淆。
