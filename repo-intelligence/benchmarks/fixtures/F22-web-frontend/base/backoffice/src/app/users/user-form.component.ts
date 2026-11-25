import { Component } from "@angular/core";
import { HttpClient } from "@angular/common/http";
import { FormBuilder, Validators } from "@angular/forms";
import { Router } from "@angular/router";

@Component({
  selector: "app-user-form",
  template: `
    <form [formGroup]="form" (ngSubmit)="save()">
      <input formControlName="email" type="email" required />
      <select formControlName="role">
        <option value="support">Support</option>
        <option value="admin">Administrator</option>
      </select>
      <button type="submit">Save</button>
      <a routerLink="/backoffice/users">Cancel</a>
    </form>
  `,
})
export class UserFormComponent {
  form = this.fb.group({ email: ["", Validators.required], role: ["support"] });

  constructor(private fb: FormBuilder, private http: HttpClient, private router: Router) {}

  save() {
    this.http.post("/api/users", this.form.value).subscribe(() => this.router.navigate(["/backoffice/users"]));
  }
}
