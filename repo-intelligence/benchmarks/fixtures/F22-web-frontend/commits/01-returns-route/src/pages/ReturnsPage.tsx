import { useState } from "react";

export default function ReturnsPage() {
  const [reason, setReason] = useState("");

  async function startReturn(event: React.FormEvent) {
    event.preventDefault();
    await fetch("/api/returns", { method: "POST", body: JSON.stringify({ reason }) });
  }

  return (
    <form name="return" onSubmit={startReturn}>
      <input name="orderId" required />
      <textarea name="reason" value={reason} onChange={(e) => setReason(e.target.value)} />
      <button type="submit">Start return</button>
    </form>
  );
}
