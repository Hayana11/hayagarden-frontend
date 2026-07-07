import { useRef, type CSSProperties, type PointerEvent, type ReactNode } from 'react';

const DRAG_THRESHOLD = 10;

interface DragScrollRowProps {
  label: ReactNode;
  children: ReactNode;
  style?: CSSProperties;
}

export function DragScrollRow({ label, children, style }: DragScrollRowProps) {
  const trackRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ x: number; left: number; moved: boolean; pointerId: number } | null>(null);

  function onPointerDown(e: PointerEvent<HTMLDivElement>) {
    if (!trackRef.current || e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (target.closest('[data-filter-pill]')) return;
    dragRef.current = { x: e.clientX, left: trackRef.current.scrollLeft, moved: false, pointerId: e.pointerId };
  }

  function onPointerMove(e: PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || !trackRef.current || e.pointerId !== drag.pointerId) return;
    const dx = e.clientX - drag.x;
    if (!drag.moved) {
      if (Math.abs(dx) < DRAG_THRESHOLD) return;
      drag.moved = true;
      trackRef.current.setPointerCapture(e.pointerId);
    }
    trackRef.current.scrollLeft = drag.left - dx;
  }

  function endDrag(e: PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || e.pointerId !== drag.pointerId) return;
    if (drag.moved && trackRef.current) {
      try {
        trackRef.current.releasePointerCapture(e.pointerId);
      } catch {
        /* already released */
      }
    }
    dragRef.current = null;
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, ...style }}>
      {label}
      <div
        ref={trackRef}
        className="drag-scroll-row"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        {children}
      </div>
    </div>
  );
}
