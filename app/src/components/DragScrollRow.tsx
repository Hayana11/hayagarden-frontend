import { useRef, type CSSProperties, type PointerEvent, type ReactNode } from 'react';

interface DragScrollRowProps {
  label: ReactNode;
  children: ReactNode;
  style?: CSSProperties;
}

export function DragScrollRow({ label, children, style }: DragScrollRowProps) {
  const trackRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ x: number; left: number; moved: boolean } | null>(null);

  function onPointerDown(e: PointerEvent<HTMLDivElement>) {
    if (!trackRef.current || e.button !== 0) return;
    dragRef.current = { x: e.clientX, left: trackRef.current.scrollLeft, moved: false };
    trackRef.current.setPointerCapture(e.pointerId);
  }

  function onPointerMove(e: PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || !trackRef.current) return;
    const dx = e.clientX - drag.x;
    if (Math.abs(dx) > 4) drag.moved = true;
    trackRef.current.scrollLeft = drag.left - dx;
  }

  function endDrag(e: PointerEvent<HTMLDivElement>) {
    if (!trackRef.current) return;
    trackRef.current.releasePointerCapture(e.pointerId);
    const moved = dragRef.current?.moved ?? false;
    dragRef.current = null;
    if (moved) {
      const blockClick = (ev: Event) => {
        ev.preventDefault();
        ev.stopPropagation();
        trackRef.current?.removeEventListener('click', blockClick, true);
      };
      trackRef.current.addEventListener('click', blockClick, true);
    }
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
