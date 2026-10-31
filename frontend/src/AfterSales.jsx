import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";

const orderStatus = { delivered: "已签收", cancelled: "已取消", paid: "已支付", shipped: "已发货" };
const refundStatus = { pending_review: "待人工审核", cancelled: "已撤回", approved: "已批准", rejected: "已拒绝", completed: "已完成" };

export default function AfterSales() {
  const [orders, setOrders] = useState([]);
  const [refunds, setRefunds] = useState([]);
  const [selected, setSelected] = useState("");
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const requestKey = useRef("");

  const refresh = useCallback(async () => {
    const [orderData, refundData] = await Promise.all([api("/api/orders"), api("/api/refunds")]);
    setOrders(orderData.orders || []);
    setRefunds(refundData.refunds || []);
  }, []);

  useEffect(() => {
    refresh().catch((err) => setError(err.message)).finally(() => setLoading(false));
  }, [refresh]);

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api("/api/refunds", {
        method: "POST",
        headers: { "Idempotency-Key": requestKey.current },
        body: JSON.stringify({ order_id: selected, reason: reason.trim() }),
      });
      setNotice(result.reused ? "已返回现有退款申请。" : "申请已提交，等待人工审核。" );
      requestKey.current = "";
      setSelected("");
      setReason("");
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function cancel(id) {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(`/api/refunds/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      setNotice("退款申请已撤回。");
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="after-sales" aria-label="订单与售后">
      <header><h2>订单与售后</h2><p className="muted">演示订单，不涉及真实交易。</p></header>
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="after-sales-notice" role="status">{notice}</p>}
      {loading ? <p className="loading-state">正在加载订单...</p> : <>
        <h3>我的订单</h3>
        <div className="after-sales-list">
          {orders.map((order) => <article className="after-sales-row" key={order.order_id}>
            <div><strong>{order.product}</strong><p>{order.order_id}</p><span>{orderStatus[order.status] || order.status} · ¥{(order.amount_cents / 100).toFixed(2)}</span></div>
            <button className="text-button" disabled={busy || order.status !== "delivered"} onClick={() => { requestKey.current = crypto.randomUUID(); setSelected(order.order_id); setReason(""); setError(""); setNotice(""); }}>申请退款</button>
          </article>)}
          {orders.length === 0 && <p className="muted">暂无订单</p>}
        </div>
        {selected && <form className="after-sales-form" onSubmit={submit}>
          <h3>退款申请</h3>
          <p>订单：{selected}</p>
          <p className="muted">演示规则：签收后 7 天内可申请，商品状态与包装需人工审核。</p>
          <label>退款原因<textarea required maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} rows={3} /></label>
          <div className="after-sales-actions"><button className="primary" disabled={busy || !reason.trim()}>提交申请</button><button type="button" className="text-button" onClick={() => setSelected("")} disabled={busy}>取消</button></div>
        </form>}
        <h3>退款申请</h3>
        <div className="after-sales-list">
          {refunds.map((refund) => <article className="after-sales-row" key={refund.id}>
            <div><strong>{refundStatus[refund.status] || refund.status}</strong><p>{refund.id}</p><span>订单 {refund.order_id}</span><p>{refund.reason}</p></div>
            {refund.status === "pending_review" && <button className="text-button" disabled={busy} onClick={() => cancel(refund.id)}>撤回申请</button>}
          </article>)}
          {refunds.length === 0 && <p className="muted">暂无退款申请</p>}
        </div>
      </>}
    </section>
  );
}
