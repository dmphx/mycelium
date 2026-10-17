import { appLogoUrl, appName } from '../branding';

export default function Brand({ className = '' }: { className?: string }) {
  if (appLogoUrl) {
    return <img src={appLogoUrl} alt={appName} className={`max-w-full object-contain ${className}`} />;
  }
  return (
    <span className="flex items-center gap-3">
      <img src="/static/mycelium-icon.svg" alt="" width="28" height="28" />
      <span className="font-mono font-bold tracking-wide text-lg text-white">{appName}</span>
    </span>
  );
}
