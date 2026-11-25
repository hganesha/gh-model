import axios from "axios";
import { Field, Form, Formik } from "formik";
import { useNavigate } from "react-router-dom";
import { setSession } from "../session";

type Credentials = { email: string; password: string };

export default function LoginPage() {
  const navigate = useNavigate();

  async function signIn(values: Credentials) {
    const { data } = await axios.post("/api/session", values);
    setSession(data.user);
    navigate("/");
  }

  return (
    <Formik initialValues={{ email: "", password: "" }} onSubmit={signIn}>
      <Form>
        <Field name="email" type="email" />
        <Field name="password" type="password" />
        <button type="submit">Sign in</button>
      </Form>
    </Formik>
  );
}
