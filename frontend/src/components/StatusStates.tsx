export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return <div className="empty-state">{label}</div>;
}

export function ErrorState({ message }: { message: string }) {
  return <div className="notice">Couldn't load this: {message}</div>;
}

export function EmptyState({ children }: { children: React.ReactNode }) {
  return <div className="empty-state">{children}</div>;
}
