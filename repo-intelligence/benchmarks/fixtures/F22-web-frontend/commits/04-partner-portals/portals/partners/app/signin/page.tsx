export default function SignIn() {
  return (
    <main>
      <h1>Partner sign-in</h1>
      <form action="/api/session" method="post">
        <label htmlFor="email">Email</label>
        <input id="email" name="email" type="email" />
        <button type="submit">Sign in</button>
      </form>
    </main>
  );
}
