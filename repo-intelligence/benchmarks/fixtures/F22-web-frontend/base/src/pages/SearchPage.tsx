import { useSearchParams } from "react-router-dom";

export default function SearchPage() {
  const [params] = useSearchParams();
  return <h1>Results for {params.get("q")}</h1>;
}
