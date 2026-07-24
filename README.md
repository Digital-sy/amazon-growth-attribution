# Amazon Growth Attribution

亚马逊流量来源与增长归因项目，用于整合订单、广告、站内促销、站外推广和价格数据，分析不同流量来源带来的订单与销售表现。

## 当前目标

第一阶段建立 Amazon All Orders Report 的原始数据入库流程，并在订单商品明细层形成可追溯的数据基础。

后续按照以下互斥优先级生成订单来源分类：

```text
站外推广 > 广告 > 站内促销 > 低价 > 自然
```

其中：

- 站外推广、站内促销、低价可根据订单与促销字段直接判断；
- 广告由于缺少订单号级归因，将按店铺、时间和 Purchased ASIN 汇总数量进行可复现分配；
- 原始标签与最终互斥分类会同时保留。

## 项目结构

```text
amazon-growth-attribution/
├─ pipelines/
│  └─ orders/
│     ├─ import_amazon_all_orders.py
│     ├─ schema.sql
│     ├─ import_log.sql
│     ├─ post_import_indexes.sql
│     └─ store_map.example.json
├─ docs/
│  └─ order_attribution_rules.md
├─ .env.example
├─ .gitignore
├─ requirements.txt
└─ README.md
```

## 当前能力

- 流式读取 Seller Central 的 All Orders Report TXT；
- 保留报告全部39个原始字段；
- 自动清洗表头空格和 UTF-8 BOM；
- 分批写入阿里云 RDS MySQL；
- 使用订单商品唯一键实现幂等更新；
- 记录来源文件、SHA256、源行号和导入批次；
- 数据库断线、锁等待和死锁自动重试；
- 支持文件结构校验和目录批量导入。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

校验文件：

```powershell
python pipelines/orders/import_amazon_all_orders.py `
  --input "D:\AmazonOrders\SY2606.txt" `
  --store-name "SY-US" `
  --validate-only
```

首次建表并导入：

```powershell
python pipelines/orders/import_amazon_all_orders.py `
  --input "D:\AmazonOrders\SY2606.txt" `
  --store-name "SY-US" `
  --create-table
```

批量导入目录：

```powershell
python pipelines/orders/import_amazon_all_orders.py `
  --input "D:\AmazonOrders" `
  --store-map "pipelines/orders/store_map.example.json" `
  --create-table
```

## 安全说明

仓库只保存代码、SQL和示例配置。以下内容禁止提交：

- `.env` 和数据库密码；
- Amazon TXT、CSV、XLSX原始报告；
- 导入日志、失败数据和导出结果；
- 包含买家地址或身份字段的任何真实数据。

当前仓库是公开仓库。由于代码本身不包含真实数据或密钥，可以作为代码备份使用；若未来增加内部规则、真实字段样例或业务文档，建议改为私有仓库。
