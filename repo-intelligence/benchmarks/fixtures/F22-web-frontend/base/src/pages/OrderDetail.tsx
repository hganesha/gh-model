import { useParams } from "react-router-dom";

export default function OrderDetail() {
  const { orderId } = useParams();
  return <h1>Order {orderId}</h1>;
}
