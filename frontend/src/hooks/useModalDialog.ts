import { useLayoutEffect, useRef } from 'react';

type DialogEntry = {
  node: HTMLElement;
  layer: number;
  order: number;
  suspended: () => boolean;
  restoreTo: HTMLElement | null;
};

const dialogs: DialogEntry[] = [];
let order = 0;

function topDialog(includeDisconnecting = false) {
  return dialogs.filter((entry) => (includeDisconnecting || entry.node.isConnected) && !entry.suspended())
    .sort((a, b) => b.layer - a.layer || b.order - a.order)[0];
}

function focusable(node: HTMLElement) {
  return Array.from(node.querySelectorAll<HTMLElement>(
    'button, a[href], input, select, textarea, iframe, [tabindex]',
  )).filter((item) => {
    if (item.tabIndex < 0 || item.matches(':disabled') || item.closest('[hidden], [inert]')) return false;
    for (let current: HTMLElement | null = item; current; current = current.parentElement) {
      const style = item.ownerDocument.defaultView?.getComputedStyle(current);
      if (style?.display === 'none' || style?.visibility === 'hidden') return false;
      if (current === node) break;
    }
    return true;
  });
}

function focusFirst(entry: DialogEntry) {
  (focusable(entry.node)[0] ?? entry.node).focus({ preventScroll: true });
}

/** Only the uppermost owned dialog handles Escape or contains keyboard focus. */
export function useModalDialog({ open, layer, onClose, suspended = false }: {
  open: boolean;
  layer: number;
  onClose: () => void;
  suspended?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  const suspendedRef = useRef(suspended);
  closeRef.current = onClose;
  suspendedRef.current = suspended;

  useLayoutEffect(() => {
    const node = ref.current;
    if (!open || !node) return;
    const document = node.ownerDocument;
    const entry: DialogEntry = {
      node, layer, order: ++order, suspended: () => suspendedRef.current,
      restoreTo: document.activeElement instanceof HTMLElement ? document.activeElement : null,
    };
    dialogs.push(entry);
    const onKey = (event: KeyboardEvent) => {
      if (topDialog() !== entry || event.defaultPrevented) return;
      if (event.key === 'Escape') {
        // A held key must not close the parent after its child has gone away.
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!event.repeat) closeRef.current();
      } else if (event.key === 'Tab') {
        const controls = focusable(node);
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (!first) {
          event.preventDefault(); node.focus();
        } else if (event.shiftKey && (document.activeElement === first || !node.contains(document.activeElement))) {
          event.preventDefault(); last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !node.contains(document.activeElement))) {
          event.preventDefault(); first.focus();
        }
      }
    };
    const onFocus = (event: FocusEvent) => {
      if (topDialog() === entry && !node.contains(event.target as Node)) focusFirst(entry);
    };
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('focusin', onFocus, true);
    if (topDialog() === entry) focusFirst(entry);
    return () => {
      // A component which stays mounted but renders null can lose its DOM ref
      // before layout cleanup. It still owns restoration until deregistered.
      const wasTop = topDialog(true) === entry;
      document.removeEventListener('keydown', onKey, true);
      document.removeEventListener('focusin', onFocus, true);
      // If the whole parent closes, a later child cleanup must not restore a
      // detached cast/trailer control instead of the original poster.
      for (const other of dialogs) {
        if (other !== entry && other.restoreTo && node.contains(other.restoreTo)) other.restoreTo = entry.restoreTo;
      }
      const index = dialogs.indexOf(entry);
      if (index >= 0) dialogs.splice(index, 1);
      if (!wasTop) return;
      const remaining = topDialog();
      const target = entry.restoreTo;
      if (target?.isConnected && !target.matches(':disabled') && !target.closest('[inert]')
        && (!remaining || remaining.node.contains(target))) target.focus({ preventScroll: true });
      else if (remaining) focusFirst(remaining);
    };
  }, [open, layer]);
  return ref;
}
