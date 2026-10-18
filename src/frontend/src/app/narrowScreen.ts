// Phone-width screens, where the map's sidebar becomes a sheet over the map's
// bottom. Keep in step with the 700px media query in styles.css.
export function isNarrowScreen(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(max-width: 700px)").matches
  );
}
