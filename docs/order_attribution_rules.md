# 订单来源归因规则

## 1. 分类结构

业务仍展示五类：

```text
广告
站外推广
站内促销
低价
自然
```

但五类的指标角色不同。

### 参与合计和占比的主来源

```text
站外推广 > 广告 > 低价 > 自然
```

这四类互斥，每个有效订单-SKU只进入一个主来源，可用于订单合计、销售额合计和来源占比。

### 仅作展示的标签

```text
站内促销
```

站内促销可以与广告、自然等主来源重叠。它展示订单量、销量和销售额，但不参与五类合计和占比。

## 2. 有效数据范围

订单分类只处理满足以下条件的商品明细：

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

## 3. 站外推广

当 `promotion_ids` 中任意促销ID命中 `MPC-` 时，主来源标记为站外推广。

站外推广优先于广告、低价和自然。

## 4. 广告

Amazon普通广告数据没有订单号级真实关联，因此广告订单属于统计分配结果。

V1.0生产口径使用产品表现月度 `ad_order_quantity`，按店铺+月份从非站外订单池中进行可复现统计分配。

广告候选池可以包含站内促销订单，因为广告和Coupon、Deal可能重叠。命中广告分配的订单，主来源为广告，同时仍可保留 `onsite_promotion_flag=1` 作为站内促销展示标签。

## 5. 站内促销

满足以下任一条件时，标记 `onsite_promotion_flag=1`：

- `promotion_ids` 命中 Percentage Off；
- `promotion_ids` 命中 PLM；
- `item_promotion_discount > 0`。

`ship_promotion_discount` 仅表示配送优惠，不能单独判断为站内商品促销。

站内促销不再作为互斥主来源。它只作为展示标签：

- 可以与广告重叠；
- 未归入站外或广告的站内促销订单，其主来源计入自然；
- 不参与订单来源合计；
- 不参与订单占比和销售额占比。

## 6. 低价

低价恢复为 **10美元** 口径。

只有在排除站外推广、广告和站内促销后，才判断低价：

```text
商品净成交单价
= (item_price - item_promotion_discount) / quantity

低价条件
= 商品净成交单价 <= 10 USD
```

因此，站内促销订单即使净成交单价不高于10美元，也不进入低价主来源；其主来源按前述规则进入广告或自然。

## 7. 自然

未归入站外推广、广告或低价的剩余有效订单，主来源归为自然。

自然订单包含：

- 无广告、无站外、非低价的普通订单；
- 未归入广告或站外的站内促销订单。

## 8. 月度汇总表

`dws_amz_order_source_monthly` 每个店铺月份保留五行展示：

```text
广告
站外推广
站内促销
低价
自然
```

其中：

- 广告、站外推广、低价、自然为主来源行；
- 站内促销为重叠展示行；
- 合计与占比分母必须排除站内促销行。

## 9. 统计周期

基础事实仍按月份保存，报表支持：

- 月度；
- 季度：Q1=1-3月、Q2=4-6月、Q3=7-9月、Q4=10-12月；
- 半年度：上半年=1-6月、下半年=7-12月。

季度和半年度同样只使用四个主来源计算合计和占比，站内促销仅展示。

## 10. 规则版本

```text
v5_10usd_promo_overlay_20260729
```

历史 `v4_7usd_20260728` 结果需要重新运行生产归因入口后，才能切换到当前口径。

## 11. 推荐保留字段

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

站外、站内促销和低价为规则识别；广告为统计估算。站内促销是展示标签，不应与四个主来源相加。