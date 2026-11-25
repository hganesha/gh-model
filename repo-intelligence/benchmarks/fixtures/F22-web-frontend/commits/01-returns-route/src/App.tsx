import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import RequireRole from "./components/RequireRole";
import HomePage from "./pages/HomePage";
import OrdersPage from "./pages/OrdersPage";
import NewOrderPage from "./pages/NewOrderPage";
import LoginPage from "./pages/LoginPage";
import SearchPage from "./pages/SearchPage";
import ReturnsPage from "./pages/ReturnsPage";

const AdminPage = lazy(() => import("./pages/AdminPage"));
const OrderDetail = lazy(() => import("./pages/OrderDetail"));

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<HomePage />} />
        <Route path="orders" element={<OrdersPage />} />
        <Route path="orders/new" element={<NewOrderPage />} />
        <Route path="orders/:orderId" element={<Suspense fallback={null}><OrderDetail /></Suspense>} />
        <Route path="search" element={<SearchPage />} />
        <Route path="returns" element={<ReturnsPage />} />
        <Route
          path="admin"
          element={
            <RequireRole role="admin">
              <AdminPage />
            </RequireRole>
          }
        />
        <Route path="old-orders" element={<Navigate to="/orders" replace />} />
      </Route>
      <Route path="/login" element={<LoginPage />} />
    </Routes>
  );
}
