import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import styles from "../styles/orders.module.scss";
import "../styles/tokens.css";

type Order = { id: string; customer: string; status: string };

export default function OrdersPage() {
  const { data: orders = [] } = useQuery<Order[]>({
    queryKey: ["orders"],
    queryFn: () => fetch("/api/orders").then((r) => r.json()),
  });

  return (
    <div className={styles.page}>
      <h1 style={{ color: "var(--color-primary)" }}>Orders</h1>
      <Link to="/orders/new">New order</Link>
      <table className={styles.table}>
        <tbody>
          {orders.map((o) => (
            <tr key={o.id}>
              <td>
                <Link to={`/orders/${o.id}`}>{o.id}</Link>
              </td>
              <td>{o.customer}</td>
              <td>{o.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
