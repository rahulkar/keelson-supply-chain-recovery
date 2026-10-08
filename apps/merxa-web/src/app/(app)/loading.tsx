export default function Loading() {
  return (
    <div className="mx-card" aria-busy="true">
      <div className="mx-empty" role="status">
        Loading…
      </div>
    </div>
  );
}
