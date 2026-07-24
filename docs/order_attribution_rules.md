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
```

取消、Pending、Non-Amazon、多渠道配送及零金额疑似换货或补发订单不进入五分类。

## 3. 站外推广

当 `promotion_ids` 中任意促销ID以 `MPC-` 开头时，标记为站外推广。

同一商品行即使同时出现普通 Percentage Off，也由站外优先。

## 4. 广告

Amazon普通广告报告没有订单号级归因，因此广告订单属于估算结果。

推荐分配粒度：

```text
店铺 + Purchased ASIN + 周
```

数据不足时可降级为：

```text
店铺 + Purchased ASIN + 月
```

广告候选池为全部非站外有效订单，不能提前排除站内促销订单，因为广告和Coupon、Deal可能重合。

分配结果必须保留：

- `estimated_ad_flag`
- 分配粒度
- 分配批次
- 广告归因数量来源
- 分类依据

## 5. 站内促销

未归入站外或广告，并满足以下任一条件：

- `promotion_ids` 包含已识别的站内商品促销；
- `item_promotion_discount > 0`。

`ship_promotion_discount` 仅表示配送优惠，不能单独判断为站内商品促销。

## 6. 低价

未归入前三类，且商品净成交单价低于SKU最低售价标准：

```text
商品净成交单价 = (item_price - item_promotion_discount) / quantity
```

不扣除税费、买家运费或配送优惠。

## 7. 自然

所有剩余有效订单商品明细归为自然订单。

## 8. 推荐保留字段

```text
offsite_flag
promotion_flag
low_price_flag
estimated_ad_flag
main_order_type
classification_reason
attribution_granularity
attribution_batch_no
```

站外、促销、低价为规则识别；广告为统计估算，两者不能混淆。
