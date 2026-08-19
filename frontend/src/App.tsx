import { useEffect, useState } from "react";
import { getHealth, getMe } from "./lib/api";
import { Hero, SystemSection, FeaturesSection } from "./components/landing";
import { ConsoleSection } from "./components/ConsoleSection";
import { AuthBar } from "./components/AuthBar";

export default function App() {
  const [health, setHealth] = useState("checking");
  const [healthError, setHealthError] = useState<string | null>(null);
  const [user, setUser] = useState<string | null>(null);

  useEffect(() => {
    getMe()
      .then((result) => setUser(result.username ?? null))
      .catch(() => setUser(null));
  }, []);

  useEffect(() => {
    let active = true;
    getHealth()
      .then((response) => {
        if (active) {
          setHealth(response.status);
          setHealthError(null);
        }
      })
      .catch((err) => {
        if (active) {
          setHealth("offline");
          setHealthError(
            err instanceof Error && err.message.includes("Failed to fetch")
              ? "control plane unavailable"
              : "control plane unavailable"
          );
        }
      });

    return () => {
      active = false;
    };
  }, []);

  return (
    <main className="min-h-screen bg-black text-[#E1E0CC]">
      <AuthBar user={user} onChange={setUser} />
      <Hero health={health} healthError={healthError} />
      <SystemSection />
      <FeaturesSection />
      <section id="evolution" className="bg-black px-4 pb-4 sm:px-6">
        <div className="mx-auto max-w-7xl rounded-lg border border-white/10 bg-[#101010] p-5 text-sm text-gray-400 md:p-7">
          <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
            <div>
              <p className="text-[10px] uppercase tracking-[0.35em] text-primary/65">
                Evolution gate
              </p>
              <h2 className="mt-2 text-2xl text-[#E1E0CC] md:text-3xl">
                Candidate heuristics do not ship on vibes.
              </h2>
            </div>
            <div className="grid gap-2 sm:grid-cols-3">
              {["benchmarks", "promotion gates", "rollback"].map((item) => (
                <div
                  className="rounded-full border border-white/10 bg-black/35 px-4 py-2 text-center text-primary/80"
                  key={item}
                >
                  {item}
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>
      <ConsoleSection user={user} />
      <footer className="border-t border-white/10 bg-black px-4 py-8 text-center text-xs text-gray-600 sm:px-6">
        RatsNest control plane / KiCad design review, repair, and strategy evolution
      </footer>
    </main>
  );
}
