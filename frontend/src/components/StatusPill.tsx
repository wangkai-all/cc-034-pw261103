import { stateTone } from "../labels";

interface Props {
  state: string;
  label: string;
  className?: string;
}

export function StatusPill({ state, label, className = "" }: Props) {
  return (
    <span className={`pill state-pill ${stateTone(state)} ${className}`.trim()}>
      {label}
    </span>
  );
}
