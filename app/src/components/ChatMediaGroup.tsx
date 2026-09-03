import { useCallback, useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react';
import {
  chatMediaImageUrl,
  chatMediaLayoutForCount,
  clampChatMediaIndex,
  nextChatMediaIndex,
  visibleChatMediaIndices,
  type ChatMediaItem,
} from '../lib/chatMedia';
import './ChatMediaGroup.css';

interface ChatMediaImageProps {
  item: ChatMediaItem;
  className?: string;
  onClick?: () => void;
  onError?: () => void;
}

function ChatMediaImage({ item, className = '', onClick, onError }: ChatMediaImageProps) {
  const [failed, setFailed] = useState(false);
  const url = chatMediaImageUrl(item.url);
  if (!url || failed) {
    return (
      <button type="button" className={`chat-media-image-button ${className}`} onClick={onClick} aria-label={item.alt || '图片加载失败'}>
        <span className="chat-media-fallback" role="img" aria-label={item.alt || '图片加载失败'} />
      </button>
    );
  }
  return (
    <button type="button" className={`chat-media-image-button ${className}`} onClick={onClick} aria-label={item.alt || '打开图片'}>
      <img
        src={url}
        alt={item.alt || ''}
        draggable={false}
        onError={() => {
          setFailed(true);
          onError?.();
        }}
      />
    </button>
  );
}

export interface ChatMediaGalleryProps {
  items: ChatMediaItem[];
  currentIndex: number;
  onClose: () => void;
}

export function ChatMediaGallery({ items, currentIndex, onClose }: ChatMediaGalleryProps) {
  const [index, setIndex] = useState(() => clampChatMediaIndex(currentIndex, items.length));
  const current = items[index];
  const move = useCallback((direction: -1 | 1) => {
    setIndex((value) => nextChatMediaIndex(value, items.length, direction));
  }, [items.length]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
      if (event.key === 'ArrowLeft') move(-1);
      if (event.key === 'ArrowRight') move(1);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [move, onClose]);

  if (!current) return null;
  return (
    <div className="chat-media-gallery" role="dialog" aria-modal="true" aria-label="图片预览" onClick={onClose}>
      <div className="chat-media-gallery-panel" onClick={(event) => event.stopPropagation()}>
        <button type="button" className="chat-media-gallery-close" onClick={onClose} aria-label="关闭图片预览">×</button>
        <ChatMediaImage item={current} className="chat-media-gallery-image" />
        {items.length > 1 && (
          <>
            <button type="button" className="chat-media-gallery-nav prev" onClick={() => move(-1)} disabled={index === 0} aria-label="上一张">‹</button>
            <button type="button" className="chat-media-gallery-nav next" onClick={() => move(1)} disabled={index === items.length - 1} aria-label="下一张">›</button>
            <div className="chat-media-gallery-counter">{index + 1} / {items.length}</div>
          </>
        )}
      </div>
    </div>
  );
}

export interface ChatMediaGroupProps {
  items: ChatMediaItem[];
  onOpenGallery?: (items: ChatMediaItem[], currentIndex: number) => void;
}

export function ChatMediaGroup({ items, onOpenGallery }: ChatMediaGroupProps) {
  const validItems = items.filter((item) => Boolean(chatMediaImageUrl(item.url)));
  const layout = chatMediaLayoutForCount(validItems.length);
  const [stackIndex, setStackIndex] = useState(0);
  const [dragX, setDragX] = useState(0);
  const [animating, setAnimating] = useState(false);
  const viewportRef = useRef<HTMLDivElement>(null);
  const pointerRef = useRef({
    id: -1,
    startX: 0,
    startY: 0,
    lastX: 0,
    lastTime: 0,
    horizontal: false,
    moved: false,
  });
  const lockRef = useRef(false);
  const clickSuppressedRef = useRef(false);
  const stackIndexRef = useRef(stackIndex);
  stackIndexRef.current = stackIndex;

  const openGallery = (index: number) => onOpenGallery?.(validItems, index);
  const clearAnimation = (callback?: () => void) => {
    const reduced = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    window.setTimeout(() => {
      setAnimating(false);
      lockRef.current = false;
      callback?.();
    }, reduced ? 20 : 280);
  };

  const settle = (direction: -1 | 1, width: number) => {
    if (lockRef.current) return;
    const index = stackIndexRef.current;
    const target = nextChatMediaIndex(index, validItems.length, direction);
    const canMove = target !== index;
    lockRef.current = true;
    setAnimating(true);
    if (canMove) {
      setDragX(direction < 0 ? -width : width);
      clearAnimation(() => {
        stackIndexRef.current = target;
        setStackIndex(target);
        setDragX(0);
      });
    } else {
      setDragX(direction < 0 ? -Math.min(width * 0.12, 32) : Math.min(width * 0.12, 32));
      clearAnimation(() => setDragX(0));
    }
  };

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (lockRef.current) return;
    pointerRef.current = {
      id: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      lastX: event.clientX,
      lastTime: Date.now(),
      horizontal: false,
      moved: false,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const pointer = pointerRef.current;
    if (pointer.id !== event.pointerId || lockRef.current) return;
    const dx = event.clientX - pointer.startX;
    const dy = event.clientY - pointer.startY;
    if (!pointer.horizontal) {
      if (Math.abs(dx) < 8) return;
      if (Math.abs(dx) <= Math.abs(dy) * 1.15) return;
      pointer.horizontal = true;
    }
    event.preventDefault();
    pointer.moved = Math.abs(dx) > 8;
    pointer.lastX = event.clientX;
    pointer.lastTime = Date.now();
    setDragX(dx);
  };

  const onPointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const pointer = pointerRef.current;
    if (pointer.id !== event.pointerId) return;
    if (pointer.horizontal) {
      const dx = event.clientX - pointer.startX;
      const elapsed = Math.max(1, Date.now() - pointer.lastTime);
      const velocity = (event.clientX - pointer.lastX) / elapsed;
      const width = viewportRef.current?.getBoundingClientRect().width || 240;
      clickSuppressedRef.current = pointer.moved;
      if (Math.abs(dx) >= width * 0.28 || Math.abs(velocity) >= 0.45) {
        settle(dx < 0 ? 1 : -1, width);
      } else {
        lockRef.current = true;
        setAnimating(true);
        clearAnimation(() => setDragX(0));
      }
    }
    pointerRef.current.id = -1;
  };

  if (layout === 'none') return null;
  if (layout === 'single') {
    return (
      <div className="chat-media-group chat-media-single">
        <ChatMediaImage item={validItems[0]} onClick={() => openGallery(0)} />
      </div>
    );
  }
  if (layout === 'double' || layout === 'collage') {
    return (
      <div className={`chat-media-group chat-media-${layout}`}>
        {validItems.map((item, index) => <ChatMediaImage key={`${item.url}-${index}`} item={item} onClick={() => openGallery(index)} />)}
      </div>
    );
  }

  const visible = visibleChatMediaIndices(validItems.length, stackIndex);
  const progress = Math.min(1, Math.abs(dragX) / (viewportRef.current?.getBoundingClientRect().width || 240));
  const mediaStyle = (index: number): CSSProperties => {
    const offset = index - stackIndex;
    const absProgress = progress;
    let x = 0;
    let y = 0;
    let rotate = 0;
    let scale = 1;
    let opacity = 1;
    if (offset === 1) {
      x = 7 * (1 - absProgress);
      y = 6 * (1 - absProgress);
      rotate = 1.4 * (1 - absProgress);
      scale = 0.985 + 0.015 * absProgress;
    } else if (offset === 2) {
      x = -5 * (1 - absProgress) + 7 * absProgress;
      y = 11 * (1 - absProgress) + 6 * absProgress;
      rotate = -1.6 * (1 - absProgress) + 1.4 * absProgress;
      scale = 0.97 + 0.015 * absProgress;
    } else if (offset < 0) {
      x = -7 * (1 - absProgress);
      y = -6 * (1 - absProgress);
      rotate = -1.4 * (1 - absProgress);
      scale = 0.985 + 0.015 * absProgress;
      opacity = absProgress;
    }
    if (offset === 0) x += dragX;
    return {
      zIndex: offset === 0 ? 4 : offset === 1 ? 3 : offset === 2 ? 2 : 1,
      opacity,
      transform: `translate3d(${x}px, ${y}px, 0) rotate(${rotate}deg) scale(${scale})`,
      transition: animating ? 'transform .26s ease, opacity .26s ease' : 'none',
    };
  };

  return (
    <div
      ref={viewportRef}
      className="chat-media-group chat-media-stack"
      data-testid="chat-photo-stack"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      {visible.map((index) => (
        <div key={`${validItems[index].url}-${index}`} className="chat-media-stack-layer" style={mediaStyle(index)}>
          <ChatMediaImage item={validItems[index]} onClick={() => {
            if (clickSuppressedRef.current) {
              clickSuppressedRef.current = false;
              return;
            }
            if (index === stackIndex) openGallery(index);
          }} />
        </div>
      ))}
      <div className="chat-media-stack-counter" aria-live="polite">{stackIndex + 1} / {validItems.length}</div>
    </div>
  );
}

