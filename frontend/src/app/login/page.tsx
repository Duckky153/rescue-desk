"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth-provider";
import { ApiError } from "@/lib/api";
import styles from "./page.module.css";

export default function LoginPage() {
  const { user, loading, login } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("analyst@rescuedesk.local");
  const [password, setPassword] = useState("DemoPassword!2026");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!loading && user) router.replace("/");
  }, [loading, router, user]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await login(email.trim().toLowerCase(), password);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Unable to sign in");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className={styles.page}>
      <section className={styles.story} aria-labelledby="login-heading">
        <div className={styles.brand}>
          <span>R</span>
          <strong>RescueDesk</strong>
        </div>
        <div className={styles.storyCopy}>
          <p className={styles.eyebrow}>ERP contract exit control</p>
          <h1 id="login-heading">Turn a difficult contract into an inspectable decision.</h1>
          <p>
            Every extracted obligation stays connected to its source. Every calculation is
            deterministic. Every approval belongs to a human.
          </p>
          <ul>
            <li>Page-level evidence</li>
            <li>Exact subscription remainder</li>
            <li>Review-ready decision packets</li>
          </ul>
        </div>
        <p className={styles.legal}>
          Independent demonstration. Not affiliated with Entry Inc. Not legal, accounting, or
          financial advice.
        </p>
      </section>
      <section className={styles.formSide} aria-label="Sign in">
        <form className={styles.form} onSubmit={submit}>
          <div>
            <p className={styles.eyebrow}>Protected workspace</p>
            <h2>Sign in to review matters</h2>
            <p className="muted">The local demonstration account is prefilled.</p>
          </div>
          {error ? (
            <div className="alert danger" role="alert">
              {error}
            </div>
          ) : null}
          <div className="field">
            <label htmlFor="email">Email</label>
            <input
              className="input"
              id="email"
              name="email"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              className="input"
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              minLength={12}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </div>
          <button className="button" type="submit" disabled={submitting}>
            {submitting ? "Signing in…" : "Open evidence workspace"}
          </button>
        </form>
      </section>
    </main>
  );
}
