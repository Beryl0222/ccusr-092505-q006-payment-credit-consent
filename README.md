# 支付与信贷同意账本

本项目提供支付与信贷同意账本所需的领域事件交换约定与基础校验库。接入方使用统一的聚合标识、事件版本和带时区的发生时间，保证业务事实在不同环节之间可以复核。

## 目录

- `contracts/domain.schema.json`：领域事件信封和已登记类型。
- `data/sample.json`：中文联调样例。
- `src/payment_credit_consent/contracts.py`：不依赖第三方包的基础校验器。
- `src/payment_credit_consent/ledger.py`：仅追加的事件存储，按聚合维护不可变版本序列并落盘。
- `src/payment_credit_consent/service.py`：账本核心服务（命令、幂等、状态机、重启恢复）。
- `src/payment_credit_consent/views.py`：按角色裁剪的只读视图与监管事实还原。
- `tests/`：契约边界、事件存储、核心服务与角色视图的检查。

## 核心对象与事件

聚合：`payment_order`、`credit_offer`、`consent_record`、`dispute_case`。

事件类型：`ORDER_CREATED`、`OFFER_SHOWN`、`OFFER_SUPERSEDED`、`SUITABILITY_REVIEWED`、`MANUAL_REVIEW_RESOLVED`、`CONSENT_GIVEN`、`PAYMENT_CONFIRMED`、`CREDIT_ACCEPTED`、`CREDIT_REJECTED`、`CONSENT_REVOKED`、`EARLY_SETTLED`、`DISPUTE_OPENED`、`DISPUTE_ADVANCED`。

## 业务规则

- **支付与信贷解耦**：`confirm(use_credit=False)` 直接完成支付；信贷被拒绝（`CREDIT_REJECTED`）只关闭报价，订单仍可改用其他方式支付。
- **幂等**：每个命令携带 `request_id`，同一请求重试原样返回首次决定（`replayed=True`），不产生新事件；决定记录持久化在 `requests.json`，重启后仍然有效。
- **重新确认**：报价指纹（费用明细 + 风险等级）变化时作废旧报价（`OFFER_SUPERSEDED`）并生成新版本；携带旧 `offer_version` 或旧 `display_version` 的确认会被 `reconfirmation_required` 拒绝，不能沿用旧同意。
- **逐项同意**：确认动作必须包含 `comprehensive_cost`、`risk_disclosure`、`contract_parties`、`revocation_window` 四项确认，缺一不可。
- **人工复核**：适当性评估结果为 `manual_review` 时确认被阻断，进入 `pending_work()["manual_reviews"]` 队列，由 `resolve_manual_review` 给出结论。
- **撤回与结清**：撤销窗口内可 `revoke_consent`，只影响后续动作；`early_settle` 结清剩余期款。历史账单、收款主体和还款计划始终保留可审计。
- **重启恢复**：事件落盘在 `events.jsonl`，`ConsentLedger.open()` 重放全部事件重建状态；`pending_work()` 返回未完成的确认、人工复核队列和到期提醒（含催收责任方）。

## 角色视图与监管还原

`view(role, order_id)` 按角色裁剪字段：

| 分区 | platform | lender | customer_service | regulator |
| --- | --- | --- | --- | --- |
| order.items（商品明细） | 可见 | 隐藏 | 可见 | 可见 |
| offer.suitability_detail（评估内部细节） | 隐藏 | 可见 | 隐藏 | 可见 |
| credit.disbursement（放款账户） | 隐藏 | 可见 | 隐藏 | 可见 |

`reconstruct(order_id)` 面向监管，基于事件流还原：每次展示的不可变快照（`displayed`）、每项决定及其作出者（`decisions`）、逐项同意与撤回（`consents`）、支付/放款/结清的资金流向（`fund_flow`）、合同参与方与催收责任方（`parties`）、争议处理阶段与历史（`dispute`）。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
