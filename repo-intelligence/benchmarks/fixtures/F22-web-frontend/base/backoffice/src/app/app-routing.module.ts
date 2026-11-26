import { NgModule } from "@angular/core";
import { RouterModule, Routes } from "@angular/router";
import { AuthGuard } from "./auth.guard";
import { ShellComponent } from "./shell.component";
import { DashboardComponent } from "./dashboard.component";
import { UsersComponent } from "./users/users.component";
import { UserFormComponent } from "./users/user-form.component";

const routes: Routes = [
  {
    path: "backoffice",
    component: ShellComponent,
    canActivate: [AuthGuard],
    data: { roles: ["admin", "support"] },
    children: [
      { path: "", component: DashboardComponent, title: "Dashboard" },
      { path: "users", component: UsersComponent, data: { roles: ["admin"] } },
      { path: "users/new", component: UserFormComponent },
      { path: "reports", loadChildren: () => import("./reports/reports.module").then((m) => m.ReportsModule) },
    ],
  },
  { path: "**", redirectTo: "backoffice" },
];

@NgModule({
  imports: [RouterModule.forRoot(routes)],
  exports: [RouterModule],
})
export class AppRoutingModule {}
