import { useEffect, type RefObject } from 'react';

// Side sheets are non-modal. Tab may leave them; closing returns to their opener.
export function usePanelFocus(ref: RefObject<HTMLElement | null>, open: boolean) {
  useEffect(() => {
    if (!open || !ref.current) return;
    const panel = ref.current;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panel.focus({ preventScroll: true });
    return () => {
      const focused = document.activeElement;
      if (opener?.isConnected && (focused === document.body || panel.contains(focused))) {
        opener.focus({ preventScroll: true });
      }
    };
  }, [open, ref]);
}
