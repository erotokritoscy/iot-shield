import { useSearchParams } from "react-router-dom";

/**
 * A single selection, stored in the URL rather than in component state.
 *
 * In a router-based app the URL *is* the view state, so anything kept outside it is state the
 * user can lose without being told: leaving Triage and coming back silently reset the inspector
 * to the top of the queue, and no alert was linkable into a ticket.
 *
 * `replace: true` is the load-bearing part. Without it every selection is a history entry, so
 * Back walks the inspector one alert at a time instead of leaving the page — which is never
 * what Back is asked for.
 *
 * Deliberately stores only the *id*. The fallback ("a choice that no longer exists falls back
 * to the top of the list") stays a derivation at the call site, so a refetch still cannot
 * overwrite an explicit choice.
 */
export function useUrlChoice(param: string): [string | null, (id: string) => void] {
  const [params, setParams] = useSearchParams();
  const choose = (id: string) => {
    const next = new URLSearchParams(params);
    if (id) next.set(param, id);
    else next.delete(param);
    setParams(next, { replace: true });
  };
  return [params.get(param), choose];
}
