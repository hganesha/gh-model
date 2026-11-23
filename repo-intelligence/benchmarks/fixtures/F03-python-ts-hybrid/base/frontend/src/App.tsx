import { useEffect, useState } from "react";

export function App() {
  const [items, setItems] = useState<{ id: number; name: string }[]>([]);
  useEffect(() => {
    fetch("/api/products").then((r) => r.json()).then(setItems);
  }, []);
  return <ul>{items.map((p) => <li key={p.id}>{p.name}</li>)}</ul>;
}
