import { useForm } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import { Button, TextField } from "../components/ui";
import { theme } from "../theme";

type NewOrder = { customer: string; quantity: number; status: string; notes: string };

async function createOrder(order: NewOrder) {
  const res = await fetch("/api/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(order),
  });
  if (!res.ok) throw new Error("order not saved");
}

export default function NewOrderPage() {
  const navigate = useNavigate();
  const { register, handleSubmit } = useForm<NewOrder>();

  const onSubmit = async (values: NewOrder) => {
    await createOrder(values);
    navigate("/orders");
  };

  return (
    <section style={{ color: theme.colors.text, padding: theme.space[3] }}>
      <h1>New order</h1>
      <form name="new-order" onSubmit={handleSubmit(onSubmit)}>
        <TextField label="Customer" {...register("customer", { required: true })} />
        <input type="number" {...register("quantity", { required: true, min: 1 })} />
        <select {...register("status")}>
          <option value="draft">Draft</option>
          <option value="placed">Placed</option>
        </select>
        <textarea {...register("notes", { maxLength: 500 })} />
        <Button type="submit" style={{ background: theme.colors.primary }}>
          Place order
        </Button>
      </form>
    </section>
  );
}
