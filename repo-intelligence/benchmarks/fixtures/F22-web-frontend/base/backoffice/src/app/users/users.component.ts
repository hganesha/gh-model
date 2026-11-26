import { Component } from "@angular/core";

@Component({
  selector: "app-users",
  templateUrl: "./users.component.html",
})
export class UsersComponent {
  query = "";
  users: { id: number; email: string }[] = [];

  search() {
    this.users = this.users.filter((u) => u.email.includes(this.query));
  }
}
