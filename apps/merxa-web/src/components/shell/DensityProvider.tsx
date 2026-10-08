"use client";

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

import { DENSITY_COOKIE, type Density } from "@/lib/density";

type DensityState = { density: Density; setDensity: (d: Density) => void };

const DensityContext = createContext<DensityState>({ density: "compact", setDensity: () => {} });

export function DensityProvider({ initial, children }: { initial: Density; children: ReactNode }) {
  const [density, setState] = useState<Density>(initial);
  const setDensity = useCallback((d: Density) => {
    setState(d);
    document.documentElement.dataset.density = d;
    // Presentation preference only (not security relevant); one year, whole site.
    document.cookie = `${DENSITY_COOKIE}=${d}; Path=/; Max-Age=31536000; SameSite=Lax`;
  }, []);
  const value = useMemo(() => ({ density, setDensity }), [density, setDensity]);
  return <DensityContext.Provider value={value}>{children}</DensityContext.Provider>;
}

export function useDensity(): DensityState {
  return useContext(DensityContext);
}

export function DensityToggle() {
  const { density, setDensity } = useDensity();
  return (
    <div className="mx-segmented" role="group" aria-label="Row density">
      {(["compact", "comfortable"] as const).map((d) => (
        <button key={d} type="button" aria-pressed={density === d} onClick={() => setDensity(d)}>
          {d === "compact" ? "Compact" : "Comfortable"}
        </button>
      ))}
    </div>
  );
}
