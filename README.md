# 支付与信贷同意账本

本项目提供支付与信贷同意账本：把商品支付、信贷报价、费用明细、适当性评估、展示文案、用户逐项确认、合同参与方、催收责任和撤销窗口记录为不可变事件，并能用不可变版本重建每次点击前后的页面事实。

## 目录

- `contracts/domain.schema.json`：领域事件信封和已登记类型（17 种事件、6 类聚合）。
- `data/sample.json`：中文联调样例。
- `src/payment_credit_consent/contracts.py`：不依赖第三方包的交换层校验器。
- `src/payment_credit_consent/events.py`：事件类型常量与构造辅助。
- `src/payment_credit_consent/ledger.py`：追加式事件账本与命令决定日志。
- `src/payment_credit_consent/domain.py`：由事件折叠重建的领域状态机。
- `src/payment_credit_consent/flow.py`：确认流程命令服务（全部业务规则）。
- `src/payment_credit_consent/views.py`：平台/放款机构/客服/监管四级字段投影。
- `src/payment_credit_consent/regulator.py`：监管视角的单笔交易还原。
- `tests/`：契约、账本、流程规则、重启恢复、角色投影、监管还原的边界检查。

## 业务规则

- **支付与信贷解耦**：支付可以在没有信贷时完成（`funded_by=cash`）；信贷拒绝只改变报价状态，订单保持可支付。
- **幂等**：同一 `command_id` 重试返回原决定（原事件原样返回）；同一 `command_id` 携带不同载荷报 `idempotency_conflict`；被拒绝的决定也按原样重放。
- **同意纪元**：逐项确认绑定 `(报价版本, 风险等级)`。报价更新（`OFFER_UPDATED`）或风险等级变化（新的评估或复核结论）会推进纪元，旧纪元下的逐项确认自动失效，必须重新确认，不能沿用旧同意。
- **逐项确认**：综合确认前必须逐项确认 `comprehensive_cost`、`risk_warning`、`contract_parties`、`revocation_window` 四个条目；展示文案必须覆盖这四个条目，否则报价不予落账。
- **适当性评估**：评估出现异常（`anomalies` 非空）自动进入人工复核队列，复核结论出具前综合确认被拒绝；复核调整风险等级同样触发重新确认。
- **撤回与结清**：撤销窗口内撤回只取消未到期的还款计划，历史账单与收款主体保留可审计；提前结清关闭后续动作（新账单、新费用、还款提醒），历史事实不变。
- **最小可见**：平台看不到评估细节与还款计划，放款机构看不到购物车明细与展示文案，客服看得到用户当时看到的内容但看不到评估内部细节，监管可见全部；被裁剪的字段名列入 `redacted_fields`。

## 持久化与恢复

账本目录下两个文件：`events.jsonl`（追加式事件，按聚合版本递增）与 `commands.jsonl`（命令决定）。`ConsentFlow.restart(store_dir, schema)` 从事件折叠重建全部状态，重启后可继续：

- `pending_confirmations()`：未完成综合确认的订单及当前纪元下仍缺的确认条目；
- `pending_manual_reviews()`：待人工复核的评估异常；
- `due_reminders(now, within_hours)`：窗口内到期（含逾期）的还款提醒；
- 幂等重试：重启前已执行的命令，重试仍返回原决定。

## 监管还原

`reconstruct_transaction(flow, order_id)` 返回一笔交易的完整事实链：

- `display_history`：每一版报价对应的页面事实（展示文案、费用明细、条款、还款计划、参与方）；
- `decisions`：谁在何时作出了什么决定（评估人、复核人、用户逐项确认、支付与签约）；
- `money_flow`：订单支付、信贷放款、分期还款计划（含当前状态）、账单应收；
- `statements` / `consent`：历史账单、收款主体与同意纪元；
- `dispute`：争议工单当前阶段与处理历史。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
