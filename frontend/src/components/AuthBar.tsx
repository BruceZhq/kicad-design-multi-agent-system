import { FormEvent, useState } from "react";
import { LogIn, LogOut, User } from "lucide-react";
import { login, logout, register } from "../lib/api";

export function AuthBar({
  user,
  onChange
}: {
  user: string | null;
  onChange: (user: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      if (mode === "register") {
        const result = await register(username.trim(), password);
        setMessage(`account created (${result.role}) — now sign in`);
        setMode("login");
      } else {
        const result = await login(username.trim(), password);
        onChange(result.username ?? username.trim());
        setOpen(false);
        setPassword("");
      }
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "auth failed");
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    await logout();
    onChange(null);
  }

  return (
    <div className="fixed right-4 top-14 z-50 sm:top-4">
      {user ? (
        <div className="flex items-center gap-2 rounded-full border border-white/10 bg-[#101010]/90 px-3 py-1.5 text-xs text-gray-300 backdrop-blur">
          <User size={14} className="text-primary" />
          <span className="max-w-[120px] truncate">{user}</span>
          <button
            className="ml-1 inline-flex items-center gap-1 rounded-full border border-white/10 px-2 py-1 text-gray-400 transition hover:text-primary"
            onClick={signOut}
            type="button"
          >
            <LogOut size={13} /> Sign out
          </button>
        </div>
      ) : (
        <button
          className="inline-flex items-center gap-1.5 rounded-full border border-primary/25 bg-primary/10 px-3 py-1.5 text-xs font-bold text-primary backdrop-blur transition hover:bg-primary/20"
          onClick={() => setOpen((value) => !value)}
          type="button"
        >
          <LogIn size={14} /> Sign in
        </button>
      )}

      {open && !user ? (
        <form
          className="mt-2 w-72 rounded-lg border border-white/10 bg-[#101010] p-4 shadow-2xl"
          onSubmit={submit}
        >
          <div className="mb-3 grid grid-cols-2 gap-1 rounded-full border border-white/10 bg-black/50 p-1 text-xs font-bold">
            {(["login", "register"] as const).map((m) => (
              <button
                className={`rounded-full px-3 py-1.5 transition ${
                  mode === m ? "bg-primary text-black" : "text-gray-400"
                }`}
                key={m}
                onClick={() => setMode(m)}
                type="button"
              >
                {m === "login" ? "Sign in" : "Register"}
              </button>
            ))}
          </div>
          <input
            autoComplete="username"
            className="mb-2 w-full rounded-md border border-white/10 bg-black/60 px-3 py-2 text-sm text-primary outline-none focus:border-primary/45"
            onChange={(event) => setUsername(event.target.value)}
            placeholder="username"
            value={username}
          />
          <input
            autoComplete="current-password"
            className="mb-2 w-full rounded-md border border-white/10 bg-black/60 px-3 py-2 text-sm text-primary outline-none focus:border-primary/45"
            onChange={(event) => setPassword(event.target.value)}
            placeholder="password (min 8 chars)"
            type="password"
            value={password}
          />
          {message ? (
            <p className="mb-2 text-[11px] text-amber-200">{message}</p>
          ) : null}
          <button
            className="w-full rounded-full bg-primary px-4 py-2 text-sm font-bold text-black disabled:opacity-60"
            disabled={busy}
            type="submit"
          >
            {busy ? "..." : mode === "login" ? "Sign in" : "Create account"}
          </button>
        </form>
      ) : null}
    </div>
  );
}
