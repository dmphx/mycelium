import { useModalDialog } from '../hooks/useModalDialog';

interface TrailerModalProps {
  youtubeKey: string | null;
  title: string;
  onClose: () => void;
}

function TrailerModal({ youtubeKey, title, onClose }: TrailerModalProps) {
  const dialogRef = useModalDialog({ open: !!youtubeKey, layer: 220, onClose });

  if (!youtubeKey) return null;

  const embed = `https://www.youtube.com/embed/${youtubeKey}?rel=0&modestbranding=1&playsinline=1`;
  const watchUrl = `https://www.youtube.com/watch?v=${youtubeKey}`;

  return (
    <div
      className="fixed inset-0 z-[220] bg-black/95 backdrop-blur-sm flex items-center justify-center p-4 sm:p-8"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div ref={dialogRef} role="dialog" aria-modal="true" tabIndex={-1}
        aria-label={title ? `${title} trailer` : 'Trailer'}
        className="relative w-full max-w-4xl bg-black rounded-2xl overflow-hidden shadow-2xl">
        <button
          type="button"
          onClick={onClose}
          className="absolute top-3 right-3 z-10 w-9 h-9 rounded-full bg-black/60 hover:bg-black/80
                     text-white text-xl flex items-center justify-center"
          aria-label="Close trailer"
        >
          x
        </button>

        <div className="relative w-full" style={{ paddingTop: '56.25%' }}>
          <iframe
            key={youtubeKey}
            className="absolute inset-0 w-full h-full"
            src={embed}
            title={title ? `${title} trailer` : 'Trailer'}
            allow="accelerometer; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
            allowFullScreen
            frameBorder="0"
          />
        </div>

        <div className="flex items-center justify-between gap-3 px-5 py-3 text-xs text-muted border-t border-border bg-card">
          <span className="truncate">
            {title ? <><span className="text-white font-semibold">{title}</span> &middot; trailer</> : 'Trailer'}
          </span>
          <a
            href={watchUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="flex-shrink-0 hover:text-white transition"
          >
            Open on YouTube &rarr;
          </a>
        </div>
      </div>
    </div>
  );
}

export default TrailerModal;
