---
doc_id: POL-LOGISTICS-001
version: "1.0"
source: SupportFlow 模拟物流规则
status: published
effective_date: "2026-01-01"
expiration_date: null
scope: {region: CN, product_types: [normal, special]}
is_simulated: true
---

# 物流与配送规则

## 查询方式

用户可以通过订单号查询订单当前状态和物流信息。订单号通常由系统生成，提交查询时应尽量提供完整订单号，避免只提供商品名称或模糊描述。

## 查不到物流

如果查询不到物流信息，可能是订单刚刚创建、物流公司尚未上传记录，或者订单号填写错误。客服应先核对订单号，再根据系统查询结果向用户说明情况，不应自行编造物流公司、运输路线或预计送达时间。

## 异常处理

如果商品长时间没有收到，用户可以提交工单，由客服人员进一步核查订单和物流记录。工单创建后会生成工单编号，用户可以使用工单编号跟进处理进度。
