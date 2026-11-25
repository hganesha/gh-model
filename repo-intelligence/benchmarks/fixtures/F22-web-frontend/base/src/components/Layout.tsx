import { Link, NavLink, Outlet } from "react-router-dom";
import { useTranslation } from "react-i18next";
import "../styles/tokens.css";

export default function Layout() {
  const { t } = useTranslation();
  return (
    <div className="shell">
      <nav aria-label={t("nav.main")} style={{ borderBottom: "1px solid var(--color-primary)" }}>
        <NavLink to="/">{t("nav.home")}</NavLink>
        <NavLink to="/orders">{t("nav.orders")}</NavLink>
        <Link to="/orders/new">New order</Link>
        <a href="https://help.example.com/shop">Help</a>
      </nav>
      <main>
        <Outlet />
      </main>
    </div>
  );
}
