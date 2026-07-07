import { BackHeader } from '../components/BackHeader';
import { Card, ScreenLayout } from '../components/Card';
import { useBook } from '../hooks/useBook';

export function ReadingScreen() {
  const book = useBook();
  const pct = book ? Math.round((book.page / book.totalPages) * 100) : 0;

  return (
    <ScreenLayout>
      <BackHeader title="共读" />

      <Card style={{ padding: 24 }}>
        <div style={{ display: 'flex', gap: 18, alignItems: 'center' }}>
          <div
            style={{
              width: 76,
              height: 106,
              borderRadius: 10,
              background: '#F1E4DF',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
            }}
          >
            <span style={{ fontFamily: "'Bodoni Moda',serif", fontStyle: 'italic', fontSize: 30, color: 'var(--color-rose)' }}>K</span>
          </div>
          <div>
            <div style={{ fontSize: 19, fontWeight: 600, letterSpacing: 1 }}>{book?.title ?? '—'}</div>
            <div style={{ fontSize: 13, color: 'var(--color-text-faint)', marginTop: 4 }}>
              {book ? `${book.author} · ${book.volumeLabel}` : ''}
            </div>
            <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 13, color: 'var(--color-text-mute)', marginTop: 10 }}>
              {book ? `p.${book.page} / ${book.totalPages} · ${pct}%` : ''}
            </div>
          </div>
        </div>
        <div style={{ height: 10, borderRadius: 5, background: '#F3E7E3', marginTop: 20, overflow: 'hidden' }}>
          <div style={{ height: '100%', borderRadius: 5, background: 'var(--color-rose)', width: `${pct}%` }} />
        </div>
      </Card>

      <Card style={{ padding: 20 }}>
        <div style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>共读笔记</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 14 }}>
          {book?.notes.map((n, i) => (
            <div key={i} style={{ display: 'flex', gap: 10 }}>
              <div
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: '50%',
                  background: n.who === 'fy' ? 'var(--color-violet)' : 'var(--color-amber)',
                  marginTop: 7,
                  flexShrink: 0,
                }}
              />
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 14, lineHeight: 1.7 }}>{n.text}</div>
                <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, color: 'var(--color-text-fainter)', marginTop: 3 }}>
                  {n.who === 'fy' ? '费佳' : '哈娅'} · {n.at}
                </div>
              </div>
            </div>
          ))}
        </div>
      </Card>

      <div style={{ fontSize: 12, color: 'var(--color-text-fainter)', textAlign: 'center', letterSpacing: 1 }}>接口位：/api/books/current</div>
    </ScreenLayout>
  );
}
